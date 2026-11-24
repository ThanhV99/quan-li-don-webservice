"""Tự kiểm tra vận chuyển đơn Đã đặt + gửi alert Telegram – chạy ở WEB (trước đây là vòng lặp trong bot).

Mỗi đơn Đã đặt có mã tra được có giờ tra riêng (orders.vc_next_check), giữ nguyên quy tắc cũ của bot:
đơn thường mỗi TRACK_INTERVAL_HOURS ± alert_rules.CHECK_JITTER_MINUTES, sắp giao NEAR_CHECK_HOURS ± …,
về kho cuối HUB_CHECK_HOURS ± …, kho cuối đã phân loại HUB_SORTED_CHECK_HOURS ± …; đơn mới có mã → lần đầu
trong FIRST_CHECK_MINUTES. Giờ hẹn + chữ ký mốc đã báo (vc_alert_last) + bảng đang giao (meta.delivering_ids)
đều nằm trong database → mỗi lượt chạy (run_pass) tiếp đúng chỗ lượt trước, dù lượt đó chạy ở đâu:
  - web Docker: SHIPMENT_WATCH=1 → watch_loop() chạy nền, ngủ tới giờ hẹn kế tiếp (tối đa 10 phút);
  - Vercel (không chạy nền được): cron ngoài gọi GET/POST /api/cron/shipments mỗi 5–10 phút, mỗi lần 1 lượt
    có giới hạn thời gian (budget);
  - bot: /kiemtravc, /danggiao gọi shipwatch.run_pass(force=True) qua API.
Một lúc chỉ 1 lượt chạy (db.take_lease) → không báo trùng khi cron và lệnh bot chạy cùng lúc.

Chỉ GỬI tin (người dùng chọn 23/11/2026: bot Telegram không cần chạy / lắng nghe liên tục nữa): sendMessage của
Bot API tới ALERT_CHAT_IDS (trống thì ALLOWED_USER_IDS) + người đã duyệt trong alert_users. Admin quản lý ở tab
🤖 Bot alert: bảng các bot (token) + telegram_id người nhận, mỗi người nhận qua bot đã chọn
(chưa chọn / admin trong .env = ALERT_BOT_TOKEN, trống thì BOT_TOKEN).
Không có nút bấm / deep link bot: link trong tin mở trang đơn trên web
(PUBLIC_URL/don-hang?don=<id>&tra=1); đơn bị hủy / hoàn → tin cho admin kèm link mở đơn để tự chuyển Đã hủy.
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import time
from datetime import datetime

import alert_rules
import db
import net
import tracking
import track_text

log = logging.getLogger(__name__)

TRACK_INTERVAL_HOURS = float(os.getenv("TRACK_INTERVAL_HOURS", "12"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALERT_BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN", "")
ALLOWED_USER_IDS = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x]
ALERT_CHAT_IDS = [int(x) for x in os.getenv("ALERT_CHAT_IDS", "").replace(" ", "").split(",") if x]
# địa chỉ web cho link trong tin; Vercel tự có VERCEL_PROJECT_PRODUCTION_URL (tên miền production, không https://)
PUBLIC_URL = (os.getenv("PUBLIC_URL", "").strip()
              or (f"https://{os.environ['VERCEL_PROJECT_PRODUCTION_URL']}" if os.getenv("VERCEL_PROJECT_PRODUCTION_URL") else "")
              ).rstrip("/")

VC_NORMAL, VC_NEAR, VC_HUB, VC_HUB_SORTED = tracking.VC_NORMAL, tracking.VC_NEAR, tracking.VC_HUB, tracking.VC_HUB_SORTED
DELIVERING_META = "delivering_ids"  # meta: {id: 'giao'|'sap'} các đơn trong bảng tổng hợp gửi gần nhất
LEASE_KEY = "shipwatch_lease"
RELEVEL_META = "shipwatch_relevel"  # meta: dấu của alert_rules lần tính lại mức gần nhất
WATCH_MAX_SLEEP = 600  # vòng lặp Docker thức dậy ít nhất 10 phút/lần để thấy đơn mới / đơn đổi trạng thái
ORDER_GAP = 2  # giây giữa 2 đơn (GHN 429, SPX)
ORDER_COST = 12  # giây ước tính cho 1 đơn (chờ lượt API + hãng trả chậm): còn ít hơn thì dừng lượt (budget)


def enabled() -> bool:
    """Web Docker có tự chạy vòng lặp không (SHIPMENT_WATCH=1). Vercel không chạy nền: dùng cron."""
    return os.getenv("SHIPMENT_WATCH", "").strip().lower() in ("1", "true", "yes", "on") and not os.getenv("VERCEL")


# ---------- Telegram ----------

def order_url(order_id: int) -> str:
    """Trang đơn trên web, mở luôn hành trình đã lưu (JS start(): ?don=<id>&tra=1). Chưa có PUBLIC_URL → ''."""
    return f"{PUBLIC_URL}/don-hang?don={order_id}&tra=1" if PUBLIC_URL else ""


async def tg(token: str, method: str, **payload) -> dict:
    resp = await net.request("POST", f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description') or resp.status_code}")
    return data["result"]


def _default_token() -> tuple[str, str]:
    """(token, nguồn) của bot mặc định – cho admin trong .env và người nhận chưa chọn bot:
    ALERT_BOT_TOKEN ("env") > bot chính BOT_TOKEN ("main"). Tên bắt đầu "_" → không lộ qua API dịch vụ của bot."""
    if ALERT_BOT_TOKEN:
        return ALERT_BOT_TOKEN, "env"
    return (BOT_TOKEN, "main") if BOT_TOKEN else ("", "")


def admin_ids() -> list[int]:
    return list(ALERT_CHAT_IDS or ALLOWED_USER_IDS)


def alert_recipients() -> list[int]:
    """Admin + người đã được duyệt trong bảng alert_users."""
    chats = dict.fromkeys(admin_ids())
    for u in db.list_alert_users(db.ALERT_APPROVED):
        chats.setdefault(u["chat_id"])
    return list(chats)


def _alert_targets(chat_ids: list[int] | None = None) -> list[tuple[int, str]]:
    """[(chat_id, token bot gửi)]: người nhận đã chọn bot (tab 🤖 Bot alert) thì gửi bằng bot đó, còn lại bot mặc định."""
    bots = {b["id"]: b["token"] for b in db.list_alert_bots()}
    users = {u["chat_id"]: u for u in db.list_alert_users()}
    default = _default_token()[0]
    out = []
    for chat_id in (alert_recipients() if chat_ids is None else chat_ids):
        u = users.get(chat_id)
        if token := (u and u["bot_id"] and bots.get(u["bot_id"])) or default:
            out.append((chat_id, token))
    return out


async def send_alert(text: str, chat_ids: list[int] | None = None) -> int:
    """Gửi tới mọi người nhận (hoặc chat_ids), mỗi người bằng bot của mình. Trả về số tin gửi được."""
    targets = _alert_targets(chat_ids)
    if not targets:
        log.warning("Chưa có bot alert (tab Bot alert / ALERT_BOT_TOKEN / BOT_TOKEN) hoặc người nhận "
                    "(tab Bot alert / ALERT_CHAT_IDS / ALLOWED_USER_IDS) để gửi alert")
        return 0
    sent = 0
    for chat_id, token in targets:
        try:
            await tg(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
            sent += 1
        except Exception as e:  # noqa: BLE001 – vd người nhận chưa bấm Start với bot alert
            log.warning("Gửi alert tới %s lỗi: %s", chat_id, str(e).replace(token, "***"))
    return sent


# ---------- lịch tra ----------

def trackable_orders() -> list:
    """Đơn Đã đặt có mã vận đơn tra được."""
    return [r for r in db.orders_by_status([db.STATUS_ORDERED])
            if r["ma_don"] and tracking.is_supported(r["ma_don"], r["don_vi_vc"])]


def check_interval(level: int) -> tuple[float, float]:
    """(chu kỳ, độ lệch ngẫu nhiên tối đa) tính bằng giây cho đơn thường / sắp giao / đã về kho cuối."""
    if level == VC_NEAR:
        iv, jitter = alert_rules.NEAR_CHECK_HOURS * 3600, alert_rules.NEAR_CHECK_JITTER_MINUTES * 60
    elif level == VC_HUB:
        iv, jitter = alert_rules.HUB_CHECK_HOURS * 3600, alert_rules.HUB_CHECK_JITTER_MINUTES * 60
    elif level == VC_HUB_SORTED:
        iv, jitter = alert_rules.HUB_SORTED_CHECK_HOURS * 3600, alert_rules.HUB_SORTED_CHECK_JITTER_MINUTES * 60
    else:
        iv, jitter = TRACK_INTERVAL_HOURS * 3600, alert_rules.CHECK_JITTER_MINUTES * 60
    return iv, min(jitter, iv / 2)


def schedule_next_check(order_id: int, level: int):
    iv, jitter = check_interval(level)
    db.set_vc_next(order_id, datetime.now().timestamp() + iv + random.uniform(-jitter, jitter))


def _rules_signature() -> str:
    """Dấu các hằng số trong alert_rules (từ khóa, nơi nhận từ .env, chu kỳ) + chu kỳ thường."""
    consts = {k: repr(v) for k, v in sorted(vars(alert_rules).items()) if k.isupper()}
    return hashlib.sha256(json.dumps([consts, TRACK_INTERVAL_HOURS]).encode()).hexdigest()[:16]


def relevel_from_logs(orders=None) -> int:
    """Tính lại mức (vc_near) của đơn Đã đặt từ hành trình đã lưu, không gọi API hãng → sửa alert_rules (từ khóa
    sắp giao / kho cuối) là nhãn trên web đúng ngay. Lên mức tra dày hơn thì dời giờ tra kế tiếp về trong 1 chu kỳ
    mới. Trả về số đơn đổi mức."""
    changed = 0
    now = datetime.now().timestamp()
    for r in trackable_orders() if orders is None else orders:
        lg = db.get_track_log(tracking.log_key(r["ma_don"]))
        if not lg:
            continue
        level = tracking.delivery_level(tracking.result_from_dict(json.loads(lg["data"])))
        if level == r["vc_near"]:
            continue
        db.set_vc_near(r["id"], level)
        iv, _ = check_interval(level)
        if r["vc_next_check"] and r["vc_next_check"] > now + iv:
            db.set_vc_next(r["id"], now + random.uniform(0, iv))
        changed += 1
    return changed


def relevel_if_rules_changed(orders) -> int:
    """Chỉ tính lại khi alert_rules / .env đổi so với lần trước (Vercel: mỗi lượt cron là 1 lần khởi động)."""
    sig = _rules_signature()
    if db.get_meta(RELEVEL_META) == sig:
        return 0
    n = relevel_from_logs(orders)
    db.set_meta(RELEVEL_META, sig)
    if n:
        log.info("Tính lại mức sắp giao / kho cuối theo hành trình đã lưu: %d đơn đổi mức", n)
    return n


# ---------- tra 1 đơn + alert ----------

async def check_order(r, stats: dict):
    """Tra 1 đơn, cập nhật cờ sắp giao + hẹn giờ tra lần tới; khớp alert_rules và có mốc mới thì gửi alert."""
    res = await tracking.track_saved(r["ma_don"], r["don_vi_vc"], r["sdt"])
    stats["checked"] += 1
    if not res.ok:
        # tra hãng lỗi (vd GHN bắt captcha) → dùng hành trình đã lưu (tiện ích Chrome / web gửi về) để vẫn tính mức
        # + gửi alert; chữ ký mốc (vc_alert_last) chặn báo trùng
        stats["errors"] += 1
        res = tracking.with_saved_fallback(res)
        if not res.ok:
            schedule_next_check(r["id"], r["vc_near"])
            return
    level = r["vc_near"]
    if res.ok and res.events:
        level = tracking.delivery_level(res)
        if level != r["vc_near"]:
            db.set_vc_near(r["id"], level)
    schedule_next_check(r["id"], level)
    if not res.events:
        return
    stats["near"] += level == VC_NEAR
    stats["hub"] += level in (VC_HUB, VC_HUB_SORTED)
    canceled = track_text.cancel_match(res)
    if not canceled and not track_text.alert_match(res):
        return
    stats["matched"] += 1
    stats["canceled"] += bool(canceled)
    latest = res.events[0]
    signature = f"{int(latest.time.timestamp())}|{latest.code}|{latest.description}"
    if r["vc_alert_last"] == signature:
        return  # đã báo mốc này rồi
    if await send_alert(track_text.alert_text(res, r, order_url)):
        db.set_vc_alert(r["id"], signature)
        stats["alerted"] += 1
        if canceled:  # chỉ admin: nhắc tự chuyển Đã hủy trên web (không tự đổi trạng thái)
            await send_alert(track_text.cancel_prompt_text(r, res, order_url), admin_ids())


# ---------- tổng hợp đơn đang giao ----------

def delivering_orders() -> list[tuple]:
    """Đơn ⏳ Đã đặt đang sắp giao / đang giao (mức VC_NEAR tính từ hành trình đã lưu) kèm hành trình (không gọi API hãng)."""
    out = []
    for r in trackable_orders():
        lg = db.get_track_log(tracking.log_key(r["ma_don"]))
        res = tracking.result_from_dict(json.loads(lg["data"])) if lg else None
        # theo hành trình đã lưu (có thể vừa tra trên web); chưa có bản lưu thì theo cờ vc_near
        level = tracking.delivery_level(res) if res and res.events else r["vc_near"]
        if level != VC_NEAR or (res and (lg["final"] or tracking.is_delivered(res))):
            continue
        out.append((r, res))
    # mốc hành trình mới nhất trước (đơn vừa chuyển sang đang giao lên đầu)
    return sorted(out, key=lambda x: x[1].events[0].time if x[1] and x[1].events else datetime.min, reverse=True)


def delivering_messages(link_user: str = "", errors: int | None = 0) -> list[str]:
    """Bảng tổng hợp đơn đang giao (các tin ≤ 4000 ký tự); link_user = @bot mở hành trình khi bấm mã (bot cũ),
    trống = link trang đơn trên web. errors: số đơn tra lại lỗi (None = cả lượt lỗi) → ghi chú dưới tiêu đề."""
    blocks = track_text.fmt_delivering(delivering_orders(), link_user or order_url, mine=db.my_phones())
    if errors is None or errors:
        blocks[0] += f"\n⚠️ Tra lại lỗi {errors if errors is not None else 'cả lượt'} đơn – đơn đó dùng hành trình đã lưu"
    return track_text.join_blocks(blocks)


async def notify_delivering():
    """Có đơn mới vào danh sách, hoặc đơn chuyển từ sắp giao sang đang giao (so với lần trước, lưu ở meta
    {id: 'giao'|'sap'}) → bot alert gửi lại cả bảng tổng hợp."""
    items = delivering_orders()
    cur = {str(r["id"]): track_text.delivering_state(res) for r, res in items}
    try:
        prev = json.loads(db.get_meta(DELIVERING_META) or "{}")
        prev = prev if isinstance(prev, dict) else {str(i): "sap" for i in prev}  # bản cũ: list id
    except ValueError:
        prev = {}
    # đơn mới vào danh sách, hoặc từ 📮 sắp giao chuyển sang 🏍 đang giao → gửi lại bảng
    if new := {int(i) for i, st in cur.items() if i not in prev or (st == "giao" and prev[i] != "giao")}:
        sent = 0
        for msg in track_text.join_blocks(track_text.fmt_delivering(items, order_url, new, db.my_phones())):
            sent += await send_alert(msg)
        if not sent:
            return  # gửi lỗi → lần sau thử lại
    if cur != prev:
        db.set_meta(DELIVERING_META, json.dumps(cur))


async def refresh_unfinished_logs(deadline: float | None = None) -> int:
    """Đơn đã rời Đã đặt nhưng hành trình lưu chưa kết thúc: tra lại (chỉ cập nhật track_logs, không alert)."""
    n = 0
    for r in db.unfinished_logs(alert_rules.UNFINISHED_LOG_HOURS):
        if deadline and time.monotonic() + ORDER_COST > deadline:
            break
        try:
            res = await tracking.track_saved(r["ma_don"], r["don_vi_vc"], r["sdt"])
            log.info("Cập nhật hành trình đơn #%s (%s): %s", r["id"], r["trang_thai"], res.milestone or res.error)
        except Exception:
            log.exception("Lỗi khi cập nhật hành trình đơn #%s", r["id"])
        n += 1
        await asyncio.sleep(ORDER_GAP)
    return n


# ---------- 1 lượt ----------

async def run_pass(force: bool = False, budget: float | None = None, wait: float = 0) -> dict:
    """1 lượt kiểm tra: hẹn giờ đơn mới, tra các đơn tới hạn (force = mọi đơn Đã đặt, cho /kiemtravc / /danggiao),
    gửi alert, gửi bảng đang giao, còn thời gian thì cập nhật hành trình đơn đã rời Đã đặt.

    budget = giới hạn giây (Vercel); hết giờ thì dừng, đơn chưa tra giữ giờ hẹn → lượt sau tra tiếp.
    wait = chờ tối đa bao nhiêu giây nếu đang có lượt khác chạy (lệnh bot); 0 = bỏ qua luôn (cron).
    Trả về thống kê + next_check (giờ hẹn sớm nhất còn lại, epoch) + busy (lượt khác đang chạy)."""
    started = time.monotonic()
    deadline = started + budget if budget else None
    stats = {"checked": 0, "near": 0, "hub": 0, "matched": 0, "canceled": 0, "alerted": 0, "errors": 0,
             "due": 0, "left": 0, "refreshed": 0, "busy": False, "next_check": None}
    lease_s = (budget or 1800) + 120
    lease = db.take_lease(LEASE_KEY, lease_s)
    while not lease and time.monotonic() - started < wait:
        await asyncio.sleep(3)
        lease = db.take_lease(LEASE_KEY, lease_s)
    if not lease:
        stats["busy"] = True
        return stats
    try:
        orders = trackable_orders()
        try:
            relevel_if_rules_changed(orders)
            orders = trackable_orders()
        except Exception:
            log.exception("Lỗi khi tính lại mức theo hành trình đã lưu")
        now = datetime.now().timestamp()
        due, upcoming = [], []
        for r in orders:
            nxt = r["vc_next_check"]
            if nxt is None:  # đơn mới có mã: tra lần đầu sớm (trong FIRST_CHECK_MINUTES), không đợi cả chu kỳ
                iv, _ = check_interval(r["vc_near"])
                nxt = now + random.uniform(0, min(iv, alert_rules.FIRST_CHECK_MINUTES * 60))
                db.set_vc_next(r["id"], nxt)
            (due if force or nxt <= now else upcoming).append((nxt, r))
        due.sort(key=lambda x: x[0])
        stats["due"] = len(due)
        done = 0
        for i, (_, r) in enumerate(due):
            if deadline and time.monotonic() + ORDER_COST > deadline:
                break
            if i:
                await asyncio.sleep(ORDER_GAP)
            try:
                await check_order(r, stats)
            except Exception:
                log.exception("Lỗi khi tra vận đơn đơn #%s", r["id"])
                stats["errors"] += 1
                schedule_next_check(r["id"], r["vc_near"])  # không để đơn lỗi bị tra lại liên tục
            done += 1
        stats["left"] = len(due) - done
        if done:
            try:
                await notify_delivering()
            except Exception:
                log.exception("Lỗi khi gửi tổng hợp đơn đang giao")
        if not stats["left"]:
            stats["refreshed"] = await refresh_unfinished_logs(deadline)
        nxt = [r["vc_next_check"] for r in trackable_orders() if r["vc_next_check"]]
        stats["next_check"] = min(nxt) if nxt else None
        if done or stats["refreshed"]:
            log.info("Kiểm tra vận chuyển: %s", {k: v for k, v in stats.items() if v})
        return stats
    finally:
        db.release_lease(LEASE_KEY, lease)


async def watch_loop():
    """Web Docker (SHIPMENT_WATCH=1): chạy lượt, ngủ tới giờ hẹn kế tiếp (tối đa WATCH_MAX_SLEEP)."""
    logged_next = None
    while True:
        try:
            s = await run_pass()
        except Exception:
            log.exception("Lỗi khi kiểm tra vận chuyển")
            s = {"next_check": None, "busy": False}
        nxt = s["next_check"]
        if nxt != logged_next and not s["busy"]:
            logged_next = nxt
            log.info("Kiểm tra vận chuyển tiếp theo lúc %s",
                     datetime.fromtimestamp(nxt).strftime("%d/%m %H:%M") if nxt else "– (không có đơn nào cần tra)")
        now = datetime.now().timestamp()
        await asyncio.sleep(max(5.0, min(WATCH_MAX_SLEEP, (nxt or now + WATCH_MAX_SLEEP) - now)))
