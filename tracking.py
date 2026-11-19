"""Tra cứu hành trình vận chuyển (nguồn không chính thức, hãng có thể đổi bất cứ lúc nào).

- SPX (Shopee Express): API JSON mà trang spx.vn dùng, không cần đăng nhập.
- J&T Express: trang HTML jtexpress.vn/vi/tracking, cần 4 số cuối SĐT người gửi hoặc người nhận.
- BEST Express: trang tra cứu có captcha nên bot không tra – chỉ tạo link best-inc.vn/track?bills=<mã>
  (trang tự điền mã và bấm tra, người dùng chỉ cần kéo captcha).
- GHN: API JSON của trang tra cứu ghn.vn, cần 4 số cuối SĐT (gửi dạng sha256("<mã>|<4 số>")).
"""
import asyncio
import hashlib
import html
import json
import random
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

import httpx

import alert_rules
import db
import net

SPX_URL = "https://spx.vn/shipment/order/open/order/get_order_info"
JT_URL = "https://jtexpress.vn/vi/tracking"
BEST_WEB_URL = "https://www.best-inc.vn/track?bills={code}"
# trang tra cứu GHN (từ 17/11/2026 API GHN bắt giải captcha: "CAPTCHA_PASS_REQUIRED") → mở web tự xem
GHN_WEB_URL = "https://donhang.ghn.vn/?order_code={code}"
GHN_URL = "https://fe-online-gateway.ghn.vn/order-tracking/public-api/client/tracking-logs"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# milestone_code của SPX → tên tiếng Việt (mã khác thì dùng milestone_name gốc)
SPX_MILESTONES = {1: "Chuẩn bị hàng", 5: "Đang vận chuyển", 6: "Đang giao hàng", 8: "Đã giao hàng"}


@dataclass
class TrackEvent:
    time: datetime
    description: str
    location: str = ""
    code: str = ""


@dataclass
class TrackResult:
    carrier: str
    tracking_no: str
    ok: bool
    error: str = ""
    milestone: str = ""
    eta: datetime | None = None  # ngày dự kiến giao (GHN có)
    web_url: str = ""  # hãng không tra được bằng bot: link mở trang tra cứu đã điền sẵn mã
    events: list[TrackEvent] = field(default_factory=list)  # mới nhất trước
    saved_at: str = ""  # lấy từ bảng track_logs (hành trình đã kết thúc), không gọi API: giờ lưu "YYYY-MM-DD HH:MM:SS"
    stale: bool = False  # tra hãng lỗi, đang trả bản lưu cũ (with_saved_fallback)


def is_spx(tracking_no: str | None, carrier: str | None = None) -> bool:
    code = (tracking_no or "").strip().upper()
    return code.startswith("SPXVN") or (bool(code) and "spx" in (carrier or "").lower())


def is_jt(tracking_no: str | None, carrier: str | None = None) -> bool:
    code = (tracking_no or "").strip()
    name = (carrier or "").lower().replace(" ", "")
    return bool(code) and ("j&t" in name or name in ("jt", "jtexpress") or
                           (not carrier and re.fullmatch(r"8\d{11}", code) is not None))


def is_ghn(tracking_no: str | None, carrier: str | None = None) -> bool:
    code = (tracking_no or "").strip().upper()
    name = (carrier or "").lower()
    return bool(code) and ("ghn" in name or "giao hàng nhanh" in name or
                           (not carrier and re.fullmatch(r"G[A-Z0-9]{7}", code) is not None))


def is_best(tracking_no: str | None, carrier: str | None = None) -> bool:
    code = (tracking_no or "").strip().upper()
    return bool(code) and ("best" in (carrier or "").lower() or (not carrier and code.startswith("TTVN")))


def web_url(tracking_no: str | None, carrier: str | None = None) -> str:
    """Link trang tra cứu của hãng (đã điền sẵn mã) cho hãng bot không tự tra được."""
    if is_best(tracking_no, carrier):
        return BEST_WEB_URL.format(code=quote(tracking_no.strip().upper()))
    return ""


def site_url(tracking_no: str | None, carrier: str | None = None) -> str:
    """Link trang tra cứu của hãng để người dùng tự xem (BEST, GHN); "" = không có."""
    if url := web_url(tracking_no, carrier):
        return url
    if is_ghn(tracking_no, carrier):
        return GHN_WEB_URL.format(code=quote(tracking_no.strip().upper()))
    return ""


def is_supported(tracking_no: str | None, carrier: str | None = None) -> bool:
    return is_spx(tracking_no, carrier) or is_jt(tracking_no, carrier) or is_ghn(tracking_no, carrier)


def phone_last4(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:] if len(digits) >= 4 else ""


async def track_spx(tracking_no: str) -> TrackResult:
    code = tracking_no.strip().upper()
    res = TrackResult("SPX", code, ok=False)
    try:
        resp = await net.request("GET", SPX_URL, headers=HEADERS,
                                 params={"spx_tn": code, "language_code": "vi"})
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        res.error = f"Không gọi được SPX: {e.__class__.__name__}"
        return res
    if data.get("retcode") != 0:
        res.error = "Không tìm thấy mã vận đơn trên SPX."
        return res

    records = ((data.get("data") or {}).get("sls_tracking_info") or {}).get("records") or []
    for r in records:
        desc = " ".join((r.get("description") or r.get("tracking_name") or "").split())
        prev = res.events[-1] if res.events else None
        if prev and prev.description == desc:
            continue  # SPX hay lặp 1 dòng nhiều lần liền nhau
        res.events.append(TrackEvent(
            time=datetime.fromtimestamp(r.get("actual_time") or 0),
            description=desc,
            location=((r.get("current_location") or {}).get("location_name") or "").strip(),
            code=r.get("tracking_code") or "",
        ))
    if records:
        latest = records[0]
        res.milestone = SPX_MILESTONES.get(latest.get("milestone_code"), latest.get("milestone_name") or "")
    res.ok = True
    return res


JT_ITEM_RE = re.compile(r'class="result-vandon-item', re.S)
JT_TIME_RE = re.compile(r"(\d{2}:\d{2}:\d{2})")
JT_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
JT_TEXT_RE = re.compile(r"<div>\s*(.*?)\s*</div>", re.S)


def _jt_text(raw: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    text = text.replace("【", " ").replace("】", " ")
    return " ".join(text.split())


def _jt_milestone(desc: str) -> str:
    d = desc.lower()
    if "thành công" in d or "ký nhận" in d:
        return "Đã giao hàng"
    if "không thành công" in d or "thất bại" in d or "hoàn" in d:
        return "Có vấn đề khi giao"
    if "đang giao" in d or "phát hàng" in d or "đang phát" in d:
        return "Đang giao hàng"
    if "nhận hàng" in d and "chuyển" not in d:
        return "Đã lấy hàng"
    return "Đang vận chuyển"


async def track_jt(tracking_no: str, phone: str | None) -> TrackResult:
    code = tracking_no.strip()
    res = TrackResult("J&T", code, ok=False)
    last4 = phone_last4(phone)
    if not last4:
        res.error = ("J&T cần 4 số cuối SĐT người nhận/người gửi. Thêm SĐT cho đơn, "
                     "hoặc tra: /tracuu <mã> <4 số cuối SĐT>")
        return res
    try:
        resp = await net.request("GET", JT_URL, timeout=20, headers=HEADERS, follow_redirects=True,
                                 params={"type": "track", "billcode": code, "cellphone": last4})
        page = resp.text
    except httpx.HTTPError as e:
        res.error = f"Không gọi được J&T: {e.__class__.__name__}"
        return res

    chunks = JT_ITEM_RE.split(page)[1:]
    if not chunks:
        res.error = ("Không tìm thấy vận đơn trên J&T (kiểm tra mã và 4 số cuối SĐT "
                     f"…{last4}).")
        return res
    for chunk in chunks:
        t, d, m = JT_TIME_RE.search(chunk), JT_DATE_RE.search(chunk), JT_TEXT_RE.search(chunk)
        if not (t and d and m):
            continue
        desc = _jt_text(m[1])
        loc = re.search(r"【(?:<[^>]+>)?([^<【】]+)", m[1])  # bưu cục đầu tiên trong 【】
        res.events.append(TrackEvent(
            time=datetime.strptime(f"{d[1]} {t[1]}", "%Y-%m-%d %H:%M:%S"),
            description=desc,
            location=html.unescape(loc[1]).strip() if loc else "",
        ))
    res.events.sort(key=lambda e: e.time, reverse=True)
    if res.events:
        res.milestone = _jt_milestone(res.events[0].description)
    res.ok = True
    return res


def _utc_to_local(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s).astimezone().replace(tzinfo=None) if s else None
    except ValueError:
        return None


async def track_ghn(tracking_no: str, phone: str | None) -> TrackResult:
    code = tracking_no.strip().upper()
    res = TrackResult("GHN", code, ok=False)
    last4 = phone_last4(phone)
    if not last4:
        res.error = ("GHN cần 4 số cuối SĐT người nhận. Thêm SĐT cho đơn, "
                     "hoặc tra: /tracuu <mã> <4 số cuối SĐT>")
        return res
    verify = hashlib.sha256(f"{code}|{last4}".encode()).hexdigest()
    try:
        resp = await net.request("POST", GHN_URL, headers=HEADERS,
                                 json={"order_code": code, "phone_verify": verify})
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        res.error = f"Không gọi được GHN: {e.__class__.__name__}"
        return res
    if data.get("code") == 429 or resp.status_code == 429:
        res.error = "GHN đang giới hạn số lần tra (gọi quá nhiều), thử lại sau ít phút."
        return res
    if data.get("code") != 200:
        res.web_url = GHN_WEB_URL.format(code=quote(code))
        if "CAPTCHA" in str(data.get("code_message") or "").upper() or "captcha" in str(data.get("message") or ""):
            res.error = "GHN đang bắt giải captcha nên bot không tự tra được – bấm link bên dưới để xem trên web GHN."
        else:
            res.error = f"Không tìm thấy vận đơn trên GHN (kiểm tra mã và 4 số cuối SĐT …{last4})."
        return res

    return parse_ghn(code, data.get("data") or {})


def parse_ghn(code: str, data: dict) -> TrackResult:
    """`data` của tracking-logs GHN (API hoặc trang donhang.ghn.vn qua tiện ích Chrome) → TrackResult."""
    res = TrackResult("GHN", code.strip().upper(), ok=False)
    info = data.get("order_info") or {}
    for log in data.get("tracking_logs") or []:  # cũ → mới
        name = (log.get("status_name") or "").strip()
        detail = " ".join((log.get("content") or (log.get("location") or {}).get("address") or "").split())
        res.events.append(TrackEvent(
            time=_utc_to_local(log.get("action_at")) or datetime.min,
            description=f"{name}: {detail}" if name and detail else (name or detail),
            location=((log.get("location") or {}).get("address") or "").strip(),
            code=log.get("status") or "",
        ))
    res.events.reverse()  # mới nhất trước như các hãng khác
    res.milestone = info.get("status_name") or ""
    res.eta = _utc_to_local(info.get("leadtime"))
    res.ok = True
    return res


async def throttle(api: str):
    """Chờ tới lượt gọi API hãng `api` (spx / jt / ghn): cách lượt trước ≥ API_MIN_GAP_SECONDS, chung cho bot + web."""
    gap = alert_rules.API_MIN_GAP_SECONDS.get(api, 0)
    if gap <= 0:
        return
    try:
        wait = await asyncio.to_thread(db.reserve_slot, f"api_slot_{api}", gap)
    except Exception:  # DB bận / lỗi → vẫn tra, chỉ mất phần giãn cách
        wait = gap
    if wait > 0:
        await asyncio.sleep(wait)


async def track(tracking_no: str, carrier: str | None = None, phone: str | None = None) -> TrackResult:
    if is_spx(tracking_no, carrier):
        await throttle("spx")
        return await track_spx(tracking_no)
    if is_jt(tracking_no, carrier):
        if phone_last4(phone):  # thiếu 4 số cuối SĐT thì không gọi API → khỏi giữ lượt
            await throttle("jt")
        return await track_jt(tracking_no, phone)
    if is_ghn(tracking_no, carrier):
        if phone_last4(phone):  # thiếu 4 số cuối SĐT thì không gọi API → khỏi giữ lượt
            await throttle("ghn")
        return await track_ghn(tracking_no, phone)
    if url := web_url(tracking_no, carrier):
        return TrackResult(carrier or "BEST", tracking_no, ok=False, web_url=url,
                           error="BEST Express có captcha nên bot không tự tra được – bấm link bên dưới, "
                                 "trang đã điền sẵn mã, chỉ cần kéo xác nhận.")
    return TrackResult(carrier or "?", tracking_no, ok=False,
                       error="Chưa hỗ trợ tra cứu đơn vị vận chuyển này (hiện có SPX, J&T, GHN; BEST mở link web).")


def with_saved_fallback(res: TrackResult) -> TrackResult:
    """Tra hãng lỗi mà đã có hành trình lưu (track_logs) → trả bản lưu đó (saved_at = giờ lưu, error = lỗi lần
    tra này, stale = True) để vẫn xem được hành trình cũ. Chỉ dùng để HIỂN THỊ (bot /tracuu, web), watcher thì không."""
    if res.ok:
        return res
    row = db.get_track_log(log_key(res.tracking_no))
    if not row:
        return res
    saved = result_from_dict(json.loads(row["data"]))
    saved.saved_at = row["updated_at"] or ""
    saved.error, saved.stale = res.error, True
    saved.web_url = res.web_url or saved.web_url
    return saved


# ---------- so khớp hành trình + lưu bảng track_logs ----------

def fold(text: str | None) -> str:
    """Chuẩn hóa để so khớp hành trình: bỏ phân biệt hoa/thường, dấu tiếng Việt và khoảng trắng thừa.
    "Đang  GIAO hàng" → "dang giao hang"."""
    s = unicodedata.normalize("NFD", (text or "").lower().replace("đ", "d"))
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").split())


def match_keywords(res: TrackResult, keywords: list[str], latest_only: bool = False,
                   exclude: list[str] | None = None) -> str | None:
    """Cụm từ đầu tiên khớp với trạng thái hiện tại hoặc 1 mốc hành trình (latest_only: chỉ mốc mới nhất).
    exclude: bỏ qua mốc có chứa 1 trong các cụm này (vd "HUB": kho trung chuyển, không phải bưu cục giao)."""
    events = res.events[:1] if latest_only else res.events
    texts = [fold(res.milestone)] + [fold(f"{ev.location} {ev.description}") for ev in events]
    if exclude:
        texts = [t for t in texts if not any(fold(x) and fold(x) in t for x in exclude)]
    for kw in keywords:
        if fold(kw) and any(fold(kw) in t for t in texts):
            return kw
    return None


def is_delivered(res: TrackResult) -> bool:
    return any(fold(m) == fold(res.milestone) for m in alert_rules.DONE_MILESTONES)


def is_final(res: TrackResult) -> bool:
    """Hành trình đã kết thúc: giao xong (DONE_MILESTONES) hoặc hủy / hoàn xong (FINAL_KEYWORDS)."""
    return res.ok and bool(res.events) and (
        is_delivered(res) or match_keywords(res, alert_rules.final_keywords_for(res.carrier), latest_only=True) is not None)


# ---------- mức tra hành trình (orders.vc_near) – dùng chung bot (watcher) + web (tra tay) ----------
VC_NORMAL, VC_NEAR, VC_HUB, VC_HUB_SORTED = 0, 1, 2, 3


def hub_sorted(res: TrackResult) -> bool:
    """Ở kho cuối và đã có mốc phân loại xong (HUB_SORTED_KEYWORDS) mới hơn lần tới kho cuối gần nhất."""
    hub = [fold(k) for k in alert_rules.hub_keywords_for(res.carrier)]
    done = [fold(k) for k in alert_rules.hub_sorted_keywords_for(res.carrier)]
    texts = [fold(f"{ev.location} {ev.description}") for ev in res.events]  # mới nhất trước
    arrived = next((i for i, t in enumerate(texts) if any(k and k in t for k in hub)), None)
    return arrived is not None and any(any(k and k in t for k in done) for t in texts[:arrived])


def delivery_level(res: TrackResult) -> int:
    """VC_NEAR = sắp giao: chưa giao xong và khớp ALERT/NEAR_KEYWORDS hoặc đã tới ngày dự kiến giao;
    VC_HUB = đã về kho cuối trước bưu cục giao (HUB_KEYWORDS), VC_HUB_SORTED = ở đó đã phân loại xong; còn lại VC_NORMAL."""
    if is_delivered(res) or match_keywords(res, alert_rules.cancel_keywords_for(res.carrier)):
        return VC_NORMAL
    if (alert_rules.NEAR_WHEN_ETA_DUE and res.eta
            and (res.eta.date() - datetime.now().date()).days <= alert_rules.NEAR_ETA_DAYS):
        return VC_NEAR
    if match_keywords(res, alert_rules.keywords_for(res.carrier)) or match_keywords(
            res, alert_rules.near_keywords_for(res.carrier), exclude=alert_rules.near_exclude_for(res.carrier)):
        return VC_NEAR
    if hub_sorted(res):
        return VC_HUB_SORTED
    return VC_HUB if match_keywords(res, alert_rules.hub_keywords_for(res.carrier)) else VC_NORMAL


def out_for_delivery(res: TrackResult | None) -> bool:
    """Mốc mới nhất là đã giao cho nhân viên / đang trên đường giao (alert_rules.OUT_FOR_DELIVERY_KEYWORDS)."""
    if not res or not res.events:
        return False
    ev = res.events[0]
    text = fold(f"{ev.location} {ev.description}")
    return any(fold(k) and fold(k) in text for k in alert_rules.out_for_delivery_keywords_for(res.carrier))


def level_interval_hours(level: int) -> float | None:
    """Chu kỳ tra của mức sắp giao / kho cuối (giờ); đơn thường: None (theo TRACK_INTERVAL_HOURS của bot)."""
    return {VC_NEAR: alert_rules.NEAR_CHECK_HOURS, VC_HUB: alert_rules.HUB_CHECK_HOURS,
            VC_HUB_SORTED: alert_rules.HUB_SORTED_CHECK_HOURS}.get(level)


def relevel_order(order, res: TrackResult) -> int | None:
    """Đơn ⏳ Đã đặt vừa có hành trình (tra từ web): ghi lại orders.vc_near theo hành trình; lên mức tra dày hơn mà
    giờ tra kế tiếp của bot còn xa (hoặc chưa có) thì kéo về trong 1 chu kỳ mới → bot tra + gửi alert sớm.
    Trả về mức mới nếu có đổi."""
    if order["trang_thai"] != db.STATUS_ORDERED or not res.ok or not res.events:
        return None
    level = delivery_level(res)
    iv = level_interval_hours(level)
    nxt = order["vc_next_check"]
    now = datetime.now().timestamp()
    if iv and (not nxt or nxt > now + iv * 3600):
        db.set_vc_next(order["id"], now + random.uniform(0, iv * 3600))
    if level == order["vc_near"]:
        return None
    db.set_vc_near(order["id"], level)
    return level


def result_to_dict(res: TrackResult) -> dict:
    return {"carrier": res.carrier, "tracking_no": res.tracking_no, "milestone": res.milestone,
            "eta": res.eta.isoformat() if res.eta else None,
            "events": [{"time": e.time.isoformat(), "description": e.description, "location": e.location, "code": e.code}
                       for e in res.events]}


def result_from_dict(d: dict) -> TrackResult:
    return TrackResult(d["carrier"], d["tracking_no"], ok=True, milestone=d.get("milestone") or "",
                       eta=datetime.fromisoformat(d["eta"]) if d.get("eta") else None,
                       events=[TrackEvent(datetime.fromisoformat(e["time"]), e["description"], e.get("location") or "",
                                          e.get("code") or "") for e in d.get("events", [])])


# J&T ghi tên + SĐT nhân viên giao trong mốc "đang giao hàng" (SPX, GHN không có thông tin người giao)
COURIER_RE = re.compile(r"Nhân viên\s+(?P<name>.+?)\s+của bưu cục\s+(?P<post>.+?)\s+đang giao hàng\.?\s*(?P<phone>\+?\d[\d .]{7,})?",
                        re.I)


def courier_of(res: TrackResult) -> dict | None:
    """Người đang giao (mốc "đang giao hàng" mới nhất có tên nhân viên): {name, post, phone, time, event} hoặc None."""
    for ev in res.events:  # mới nhất trước
        if m := COURIER_RE.search(ev.description):
            phone = re.sub(r"\D", "", m["phone"] or "")
            if phone.startswith("84"):
                phone = "0" + phone[2:]
            return {"name": m["name"].strip(), "post": m["post"].strip(), "phone": phone, "time": ev.time, "event": ev}
    return None


def log_key(tracking_no: str) -> str:
    return "".join((tracking_no or "").split()).upper()


async def track_saved(tracking_no: str, carrier: str | None = None, phone: str | None = None) -> TrackResult:
    """Như track(), nhưng hành trình đã kết thúc (is_final) thì lấy từ bảng track_logs, không gọi API hãng.
    Mỗi lần tra được thì lưu lại bản mới nhất."""
    key = log_key(tracking_no)
    if key and (row := db.get_track_log(key)) and row["final"]:
        res = result_from_dict(json.loads(row["data"]))
        res.saved_at = row["updated_at"] or ""
        return res
    res = await track(tracking_no, carrier, phone)
    save_result(res, tracking_no)
    return res


def save_result(res: TrackResult, tracking_no: str | None = None):
    """Lưu hành trình vừa tra được (có mốc) vào track_logs (khóa = mã vận đơn của đơn)."""
    key = log_key(tracking_no or res.tracking_no)
    if key and res.ok and res.events:
        db.save_track_log(key, res.carrier, res.milestone, json.dumps(result_to_dict(res), ensure_ascii=False),
                          is_final(res))


# ---------- bưu cục / kho giao cuối + nhân viên giao (web tab 🏍 Giao hàng, tổng hợp từ track_logs) ----------

# mốc "tới nơi giao cuối" theo hãng: (regex lấy tên nơi, cụm loại trừ = kho trung chuyển, không phải nơi giao)
LAST_MILE_RE = {
    "J&T": (re.compile(r"Hàng đã được chuyển đến\s+(.+)", re.I), ["HUB", "TTKT", "ĐGP"]),
    "SPX": (re.compile(r"Đơn hàng đã đến kho\s+(.+)", re.I), ["Mega SOC", "Warehouse"]),
    "GHN": (re.compile(r"(?:đến bưu cục giao|giao tại)\s+(.+?)(?:\s+và sẽ.*)?$", re.I), []),
}
DELIVERY_TRY_RE = re.compile(r"đang giao hàng", re.I)


def _last_mile(res: TrackResult):
    """(tên nơi giao cuối, mốc tới nơi đó) — J&T có nhân viên giao thì lấy bưu cục của nhân viên đó."""
    if c := courier_of(res):
        arrived = next((ev for ev in reversed(res.events) if fold(c["post"]) in fold(ev.description)
                        and "chuyen den" in fold(ev.description)), c["event"])
        return c["post"], arrived
    rx, skip = LAST_MILE_RE.get(res.carrier, (None, []))
    for ev in res.events if rx else []:  # mới nhất trước
        if (m := rx.search(ev.description)) and not any(fold(s) in fold(m[1]) for s in skip):
            return " ".join(m[1].split()).rstrip("."), ev
    return None, None


def _couriers(res: TrackResult) -> list[dict]:
    """Mọi nhân viên đi giao đơn này (J&T), mới nhất trước, mỗi người 1 lần."""
    out, seen = [], set()
    for ev in res.events:
        if (m := COURIER_RE.search(ev.description)) and fold(m["name"]) not in seen:
            seen.add(fold(m["name"]))
            phone = re.sub(r"\D", "", m["phone"] or "")
            out.append({"name": m["name"].strip(), "post": m["post"].strip(),
                        "phone": "0" + phone[2:] if phone.startswith("84") else phone, "time": ev.time})
    return out


def last_mile_summary() -> list[dict]:
    """Theo hãng → nơi giao cuối (bưu cục / kho Hub) → nhân viên giao (hãng có ghi tên: J&T) + các đơn,
    tính từ hành trình đã lưu (track_logs), không gọi API. Chỉ tính đơn đã tới nơi giao cuối."""
    with db.connect() as conn:
        orders: dict[str, list] = {}
        for o in conn.execute("SELECT * FROM orders WHERE TRIM(COALESCE(ma_don, '')) != '' ORDER BY id"):
            orders.setdefault(log_key(o["ma_don"]), []).append(o)
        logs = conn.execute("SELECT * FROM track_logs ORDER BY ma_don").fetchall()  # thứ tự cố định (Postgres không theo thứ tự ghi)
    fails = [fold(k) for k in alert_rules.FAIL_KEYWORDS]
    carriers: dict[str, dict] = {}
    for lg in logs:
        res = result_from_dict(json.loads(lg["data"]))
        post, arrived = _last_mile(res)
        if not post or not res.events:
            continue
        after = [ev for ev in res.events if ev.time >= arrived.time]
        delivered = is_delivered(res)
        state = ("delivered" if delivered else "returned" if is_final(res)
                 else "giao" if out_for_delivery(res) else "post")
        couriers = _couriers(res)
        item = {"ma_don": lg["ma_don"], "state": state, "arrived": f"{arrived.time:%Y-%m-%d %H:%M}",
                "done": f"{res.events[0].time:%Y-%m-%d %H:%M}" if delivered else None,
                "hours": round((res.events[0].time - arrived.time).total_seconds() / 3600, 1) if delivered else None,
                "tries": sum(bool(DELIVERY_TRY_RE.search(ev.description)) for ev in after),
                "fails": sum(any(k in fold(ev.description) for k in fails) for ev in after),
                "latest": res.events[0].description, "courier": couriers[0]["name"] if couriers else None,
                "orders": [{"id": o["id"], "trang_thai": o["trang_thai"], "ten_nguoi_dat": o["ten_nguoi_dat"],
                            "ten_tren_don": o["ten_tren_don"], "ten_sp": o["ten_sp"], "dia_chi": o["dia_chi"]}
                           for o in orders.get(lg["ma_don"], [])]}
        c = carriers.setdefault(res.carrier, {"carrier": res.carrier, "posts": {}})
        p = c["posts"].setdefault(fold(post), {"post": post, "items": [], "couriers": {}})
        p["items"].append(item)
        for i, cr in enumerate(couriers):
            k = p["couriers"].setdefault(fold(cr["name"]), {"name": cr["name"], "phones": [], "don": 0, "delivered": 0,
                                                            "first": cr["time"], "last": cr["time"], "dia_chi": {}})
            if cr["phone"] and cr["phone"] not in k["phones"]:
                k["phones"].append(cr["phone"])
            k["don"] += 1
            k["delivered"] += i == 0 and delivered  # đơn giao xong tính cho người giao lần cuối
            k["first"], k["last"] = min(k["first"], cr["time"]), max(k["last"], cr["time"])
            for o in item["orders"]:
                if (dc := " ".join((o["dia_chi"] or "").split())):
                    k["dia_chi"][dc] = k["dia_chi"].get(dc, 0) + 1
    out = []
    for c in carriers.values():
        posts = []
        for p in c["posts"].values():
            items = sorted(p["items"], key=lambda x: (x["done"] or x["arrived"], x["ma_don"]), reverse=True)  # ngang giờ → theo mã
            hours = [x["hours"] for x in items if x["hours"] is not None]
            couriers = sorted(p["couriers"].values(), key=lambda k: (-k["don"], k["name"]))
            for k in couriers:
                k["first"], k["last"] = f"{k['first']:%Y-%m-%d}", f"{k['last']:%Y-%m-%d}"
                k["dia_chi"] = [{"dc": dc, "n": n} for dc, n in sorted(k["dia_chi"].items(), key=lambda x: (-x[1], x[0]))]
            posts.append({"post": p["post"], "don": len(items),
                          "delivered": sum(x["state"] == "delivered" for x in items),
                          "dang_giao": sum(x["state"] in ("giao", "post") for x in items),
                          "returned": sum(x["state"] == "returned" for x in items),
                          "avg_hours": round(sum(hours) / len(hours), 1) if hours else None,
                          "last": items[0]["done"] or items[0]["arrived"], "couriers": couriers, "items": items})
        posts.sort(key=lambda p: (-p["don"], p["post"]))
        out.append({"carrier": c["carrier"], "don": sum(p["don"] for p in posts),
                    "has_names": any(p["couriers"] for p in posts), "posts": posts})
    return sorted(out, key=lambda c: (-c["don"], c["carrier"]))
