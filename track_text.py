"""Chữ của tin vận chuyển (HTML Telegram): hành trình 1 đơn, tiêu đề alert, bảng tổng hợp đơn đang giao.

Module THUẦN (không database, không mạng): web dùng khi tự kiểm tra đơn + gửi alert (shipwatch.py), bot tải mã nguồn
về chạy tại chỗ (/tracuu, /danggiao) như utils / tracking. Thứ cần database / cấu hình (số của tôi, link) truyền vào
tham số. Link mã vận đơn: `link` = hàm order_id → url (web: trang đơn), hoặc chuỗi = @username bot (t.me/…?start=tr_).
"""
import html
from datetime import datetime

import alert_rules
import tracking
from utils import money, norm_phone

fold, _match, is_delivered = tracking.fold, tracking.match_keywords, tracking.is_delivered
out_for_delivery = tracking.out_for_delivery

PHONE_MINE = "📱"


def phone_tag(raw, mine: dict) -> str:
    """Sau SĐT của đơn: số của tôi (mine = db.my_phones(): {số chuẩn: tên máy}) thì ghi lắp ở máy nào."""
    norm = norm_phone(raw)
    if not norm or norm not in (mine or {}):
        return ""
    return f" · {PHONE_MINE} <i>{html.escape(mine[norm]) if mine[norm] else 'số của tôi'}</i>"


def fmt_tracking(res: "tracking.TrackResult", order=None) -> str:
    lines = [f"🚚 <b>{html.escape(res.carrier)}</b> · <code>{html.escape(res.tracking_no)}</code>"]
    if order:
        lines.append(f"🧾 Đơn #{order['id']} · 👤 {html.escape(order['ten_nguoi_dat'] or '—')}"
                     f" · 📦 {html.escape(order['ten_sp'] or '—')}")
    if not res.ok:
        lines.append(f"\n⚠️ {html.escape(res.error)}")
        if res.web_url:
            lines.append(f'🌐 <a href="{html.escape(res.web_url)}">Mở trang tra cứu {html.escape(res.carrier)}</a>')
        return "\n".join(lines)
    if not res.events:
        lines.append("\nChưa có thông tin hành trình.")
        return "\n".join(lines)
    latest = res.events[0]
    lines.append(f"\n📍 <b>{html.escape(res.milestone.upper() or latest.description.upper())}</b>")
    lines.append(f"<i>{html.escape(latest.description)} — {latest.time:%H:%M %d/%m}</i>")
    if res.eta:
        lines.append(f"📅 Dự kiến giao: <b>{res.eta:%d/%m/%Y}</b>")
    lines.append("\n<b>Hành trình:</b>")
    for ev in res.events[:10]:
        bad = "không thành công" in ev.description.lower() or "thất bại" in ev.description.lower()
        desc = f"⚠️ <b>{html.escape(ev.description)}</b>" if bad else html.escape(ev.description)
        lines.append(f"▸ {ev.time:%d/%m %H:%M} · {desc}")
    if len(res.events) > 10:
        lines.append(f"<i>… và {len(res.events) - 10} mốc cũ hơn</i>")
    if res.stale:
        t = datetime.fromisoformat(res.saved_at)
        lines.append(f"\n⚠️ Tra lại lỗi: {html.escape(res.error)}")
        lines.append(f"<i>💾 Đang xem bản lưu lúc {t:%H:%M %d/%m} – có thể đã cũ</i>")
        if res.web_url:
            lines.append(f'🌐 <a href="{html.escape(res.web_url)}">Mở trang tra cứu {html.escape(res.carrier)}</a>')
    elif res.saved_at:
        t = datetime.fromisoformat(res.saved_at)
        lines.append(f"\n<i>💾 Hành trình đã kết thúc, xem bản lưu lúc {t:%H:%M %d/%m} (không tra lại hãng)</i>")
    else:
        lines.append(f"\n<i>Cập nhật lúc {datetime.now():%H:%M %d/%m}</i>")
    return "\n".join(lines)


# ---------- alert ----------

def alert_match(res: "tracking.TrackResult") -> str | None:
    return _match(res, alert_rules.keywords_for(res.carrier))


def cancel_match(res: "tracking.TrackResult") -> str | None:
    """Cụm từ cho biết đơn bị hủy / hoàn về (alert_rules.CANCEL_KEYWORDS), None nếu không."""
    return _match(res, alert_rules.cancel_keywords_for(res.carrier))


def alert_title(res: "tracking.TrackResult", order_id: int) -> str:
    """Tiêu đề alert theo trạng thái hiện tại: 🚚 đang giao / ✅ đã giao / ❌ bị hủy, hoàn hàng / ⚠️ giao lỗi."""
    status = res.milestone or res.events[0].description
    text = fold(f"{status} {res.events[0].description}")
    if cancel_match(res):
        return f"🔔 <b>Đơn #{order_id} · ❌ BỊ HỦY / HOÀN HÀNG</b> ({html.escape(status)})"
    icon = ("✅" if is_delivered(res) else
            "⚠️" if any(fold(k) in text for k in alert_rules.FAIL_KEYWORDS) else "🚚")
    return f"🔔 <b>Đơn #{order_id} · {icon} {html.escape(status)}</b>"


def alert_text(res: "tracking.TrackResult", r, link=None) -> str:
    """Tin alert 1 đơn: tiêu đề + hành trình + link xem lại (trang đơn trên web)."""
    url = track_link(link, r["id"])
    return (alert_title(res, r["id"]) + "\n\n" + fmt_tracking(res, r)
            + (f'\n\n🌐 <a href="{html.escape(url)}">Xem đơn #{r["id"]} trên web</a>' if url else ""))


def cancel_prompt_text(r, res: "tracking.TrackResult", link=None) -> str:
    """Tin gửi admin khi hành trình báo đơn bị hủy / hoàn: tóm tắt đơn + link mở đơn trên web để chuyển Đã hủy
    (trạng thái không bao giờ tự đổi)."""
    e = lambda v: html.escape(str(v)) if v not in (None, "") else "—"
    ev = res.events[0]
    qty = r["so_luong"] or 1
    lines = [f"❌ <b>Đơn #{r['id']} bị hủy / hoàn hàng</b> theo {html.escape(res.carrier)}:",
             f"<i>{ev.time:%d/%m %H:%M} · {html.escape(ev.description)}</i>", "",
             f"🧾 <b>Đơn #{r['id']}</b> · {e(r['trang_thai'])}  <i>({e(r['created_at'])})</i>",
             f"👤 <b>{e(r['ten_nguoi_dat'])}</b>" + (f" · 🪪 {e(r['ten_tren_don'])}" if r["ten_tren_don"] else ""),
             f"📦 {e(r['ten_sp'])}{f' × {qty}' if qty > 1 else ''} · 💸 đặt <b>{money(r['gia_dat'] or 0)}</b>"
             f" + công <b>{money(r['cong_dat'] or 0)}</b>"]
    if r["sdt"]:
        lines.append(f"📞 <code>{html.escape(r['sdt'])}</code>")
    if r["dia_chi"]:
        lines.append(f"🏠 {html.escape(r['dia_chi'])}")
    lines.append(f"🚚 {e(r['don_vi_vc'])} · <code>{e(r['ma_don'])}</code>")
    url = track_link(link, r["id"])
    lines.append(f'\n👉 Chuyển đơn sang <b>Đã hủy</b>: <a href="{html.escape(url)}">mở đơn #{r["id"]} trên web</a>'
                 if url else "\nChuyển đơn sang <b>Đã hủy</b>?")
    return "\n".join(lines)


# ---------- tổng hợp đơn đang giao đến ----------

def is_fail_event(ev) -> bool:
    text = fold(ev.description)
    return any(fold(k) in text for k in alert_rules.FAIL_KEYWORDS)


def delivering_state(res) -> str:
    """'giao' = đang giao (có nhân viên đang đi giao), 'sap' = đã tới bưu cục giao cuối, chưa đi giao."""
    return "giao" if res and out_for_delivery(res) else "sap"


def track_link(link, order_id: int) -> str:
    """link = hàm order_id → url, hoặc @username bot (deep link tr_<id>), hoặc rỗng."""
    if callable(link):
        return link(order_id) or ""
    return f"https://t.me/{link}?start=tr_{order_id}" if link else ""


def fmt_delivering_order(i: int, r, res, link, new: bool, state: str = "", mine=None) -> str:
    esc = html.escape
    url = track_link(link, r["id"])
    code = f'<a href="{esc(url)}"><b>{esc(r["ma_don"])}</b></a>' if url else f"<code>{esc(r['ma_don'])}</code>"
    carrier = r["don_vi_vc"] or (res.carrier if res else "")
    qty = r["so_luong"] or 1
    # người đặt nằm ở dòng tiêu đề nhóm (fmt_delivering) → ở đây chỉ còn trạng thái giao + tên trên đơn
    tag = {"giao": " · 🏍 <b>Đang giao</b>", "sap": " · 📮 Sắp giao"}.get(state, "")
    lines = [f"{'🆕 ' if new else ''}<b>{i}. Đơn #{r['id']}</b>{tag}"
             + (f" · 🪪 {esc(r['ten_tren_don'])}" if r["ten_tren_don"] else ""),
             f"🚚 {esc(carrier or '—')} · {code}",
             f"📦 {esc(r['ten_sp'] or '—')}{f' × {qty}' if qty > 1 else ''} · 💰 {money(r['gia_dat'] or 0)}"]
    if r["sdt"]:  # SĐT trên đơn (shipper gọi số này) + số của tôi thì lắp ở máy nào
        lines.append(f"📞 <code>{esc(r['sdt'])}</code>{phone_tag(r['sdt'], mine)}")
    if r["dia_chi"]:
        lines.append(f"🏠 {esc(r['dia_chi'])}")
    courier = tracking.courier_of(res) if res else None
    latest_is_courier = bool(courier and res.events and courier["event"] == res.events[0])  # == (không phải is): mốc qua API là bản sao
    if res and res.events and not latest_is_courier:
        ev = res.events[0]
        lines.append(f"{'⚠️' if is_fail_event(ev) else '📍'} {ev.time:%H:%M %d/%m} · {esc(ev.description)}")
    if courier:
        lines.append(f"🧑‍✈️ {'' if latest_is_courier else 'Lần giao trước · '}{courier['time']:%H:%M %d/%m} · "
                     f"<b>{esc(courier['name'])}</b>" + (f" · {courier['phone']}" if courier["phone"] else "")
                     + f" · {esc(courier['post'])}")
    return "\n".join(lines)


def fmt_delivering(items: list[tuple], link, new_ids=frozenset(), mine=None) -> list[str]:
    """Các khối HTML: tiêu đề, rồi gom theo người đặt (người dùng yêu cầu): mỗi người 1 dòng tiêu đề số đơn + tổng giá
    đặt, người có đơn 🏍 Đang giao (đã có nhân viên đi giao) lên trước, rồi tới 📮 Sắp giao (đã tới bưu cục cuối);
    mỗi đơn: trạng thái giao, tên trên đơn, mã vận đơn bấm được (link: xem track_link), ĐVVC, hàng, giá đặt,
    địa chỉ, mốc mới nhất, nhân viên giao nếu hãng có ghi (J&T)."""
    head = f"🛵 <b>ĐƠN ĐANG GIAO ĐẾN ({len(items)})</b> · {datetime.now():%H:%M %d/%m}"
    if not items:
        return [head + "\n\nKhông có đơn nào đang giao."]
    states = {r["id"]: delivering_state(res) for r, res in items}
    n_giao = sum(st == "giao" for st in states.values())
    total = sum(r["gia_dat"] or 0 for r, _ in items)
    # gom theo người đặt, giữ thứ tự mốc mới nhất trước; trong mỗi người: đang giao trước sắp giao
    people: dict[str, list] = {}
    for r, res in items:
        people.setdefault(" ".join((r["ten_nguoi_dat"] or "").lower().split()), []).append((r, res))
    groups = sorted(people.values(), key=lambda g: not any(states[r["id"]] == "giao" for r, _ in g))
    blocks = [head + f"\n🏍 Đang giao: <b>{n_giao}</b> · 📮 Sắp giao: <b>{len(items) - n_giao}</b>"
              + f" · 👤 {len(groups)} người · 💰 tổng <b>{money(total)}</b>"
              + (f"\n🆕 = mới vào danh sách / vừa đi giao ({len(new_ids)} đơn)" if new_ids else "")
              + "\n<i>🏍 Đang giao = nhân viên đã nhận hàng đi giao · 📮 Sắp giao = đã tới bưu cục giao cuối</i>"
              + "\n<i>Bấm mã vận đơn để xem hành trình từng đơn</i>"]
    i = 0
    for g in groups:
        g.sort(key=lambda x: states[x[0]["id"]] != "giao")
        n = {st: sum(states[r["id"]] == st for r, _ in g) for st in ("giao", "sap")}
        detail = " · ".join(f"{icon} {n[st]}" for st, icon in (("giao", "🏍"), ("sap", "📮")) if n[st])
        blocks.append(f"━━━━━━━━━━\n👤 <b>{html.escape(g[0][0]['ten_nguoi_dat'] or '—')}</b> · {len(g)} đơn ({detail})"
                      f" · 💰 <b>{money(sum(r['gia_dat'] or 0 for r, _ in g))}</b>")
        for r, res in g:
            i += 1
            blocks.append(fmt_delivering_order(i, r, res, link, r["id"] in new_ids, states[r["id"]], mine))
    return blocks


def join_blocks(blocks: list[str], limit: int = 4000) -> list[str]:
    """Ghép các khối thành tin ≤ limit ký tự (giới hạn 4096 của Telegram), không cắt giữa 1 khối."""
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > limit:
            msgs.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    return msgs + [cur] if cur else msgs
