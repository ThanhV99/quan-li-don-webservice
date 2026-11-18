"""Điều kiện để bot alert gửi thông báo (chỉ áp dụng cho đơn ⏳ Đã đặt).

Bình thường bot tra hành trình mỗi 12 giờ (TRACK_INTERVAL_HOURS trong .env). Đơn "sắp giao"
được tra dày hơn: mỗi NEAR_CHECK_HOURS giờ, cho tới khi đã giao xong hoặc đổi trạng thái.

Bot tìm các cụm từ dưới đây trong:
  - trạng thái hiện tại của đơn (vd SPX: "Đang giao hàng", "Đã giao hàng")
  - nội dung + địa điểm của mọi mốc trong hành trình (vd tên bưu cục giao gần nơi nhận)
Không phân biệt hoa/thường, có dấu/không dấu, khoảng trắng thừa:
"đang giao hàng" = "Đang Giao Hàng" = "dang giao hang" → mỗi cụm chỉ cần ghi 1 lần.

ALERT_KEYWORDS: khớp 1 cụm là gửi alert và đơn được coi là sắp giao. Từ đó mỗi mốc hành trình mới
                (giao thành công, giao thất bại, hẹn giao lại…) cũng được báo, mỗi mốc 1 lần.
NEAR_KEYWORDS:  khớp 1 cụm thì đơn được coi là sắp giao (tra mỗi NEAR_CHECK_HOURS giờ) nhưng chưa gửi alert.
                Mốc có cụm trong NEAR_EXCLUDE_KEYWORDS (vd J&T "HUB") thì không tính.
HUB_KEYWORDS:   đơn đã về kho trung chuyển cuối (ngay trước bưu cục giao) → tra mỗi HUB_CHECK_HOURS giờ thay vì
                TRACK_INTERVAL_HOURS, không gửi alert. Sắp giao (ALERT/NEAR) vẫn ưu tiên hơn.
HUB_SORTED:     ở kho cuối mà có mốc "đã phân loại, sẵn sàng đi tiếp" / "đang chuyển hàng đến bưu cục" MỚI HƠN mốc
                tới kho → sắp rời / đã rời kho cuối, tra mỗi HUB_SORTED_CHECK_HOURS giờ (không alert).
NEAR_WHEN_ETA_DUE: hãng có ngày dự kiến giao (GHN) mà còn ≤ NEAR_ETA_DAYS ngày nữa → cũng coi là sắp giao
                   (1 = từ hôm trước ngày dự kiến: hàng hay tới bưu cục giao lúc nửa đêm và giao từ sáng sớm).

Khóa "*" áp dụng cho mọi hãng. Tên hãng phải đúng như bot hiển thị: "SPX", "J&T", "GHN".
Sửa xong file này cần build lại web (docker compose up -d --build) rồi khởi động lại bot.

Tên kho / bưu cục gần NƠI NHẬN HÀNG là thông tin riêng → đặt trong .env của web, không ghi trong code
(nhiều giá trị cách nhau bằng |), sửa xong chỉ cần khởi động lại web + bot:
  VC_ALERT_PLACES  tên kho / bưu cục giao: hành trình có tên này là gửi alert luôn (vd "20-XXX Abc 1 Hub")
  VC_HUB_SPX       kho phân loại cuối của SPX trước kho giao ("Đơn hàng đã đến kho <tên>")
  VC_HUB_JT        kho cuối của J&T ("Hàng đã được chuyển đến <tên>")
  VC_HUB_GHN       kho cuối của GHN ("… lưu tại <tên>")
  VC_JT_AREA       mã tỉnh J&T của nơi nhận, vd HCM: "Hàng đã được chuyển đến (HCM) …" = tới bưu cục giao
Để trống thì bỏ qua mức đó (chỉ còn báo theo "Đang giao hàng" chung của các hãng).
"""
import os


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split("|") if x.strip()]


ALERT_PLACES = _env_list("VC_ALERT_PLACES")
HUB_SPX = _env_list("VC_HUB_SPX")
HUB_JT = _env_list("VC_HUB_JT")
HUB_GHN = _env_list("VC_HUB_GHN")
JT_AREAS = _env_list("VC_JT_AREA")
# chưa đặt gì trong .env → web ghi cảnh báo lúc khởi động (bot sẽ không biết đơn nào về kho cuối / sắp giao)
PLACES_CONFIGURED = bool(ALERT_PLACES or HUB_SPX or HUB_JT or HUB_GHN or JT_AREAS)

NEAR_CHECK_HOURS = 1
HUB_CHECK_HOURS = 6
HUB_SORTED_CHECK_HOURS = 3
# Mỗi đơn có giờ tra riêng, cộng/trừ ngẫu nhiên tối đa chừng này phút (không quá nửa chu kỳ) cho đỡ đều đặn:
CHECK_JITTER_MINUTES = 60       # đơn Đã đặt thường (chu kỳ TRACK_INTERVAL_HOURS)
NEAR_CHECK_JITTER_MINUTES = 30  # đơn sắp giao (chu kỳ NEAR_CHECK_HOURS)
HUB_CHECK_JITTER_MINUTES = 30   # đơn đã về kho cuối (chu kỳ HUB_CHECK_HOURS)
HUB_SORTED_CHECK_JITTER_MINUTES = 20  # đơn ở kho cuối đã phân loại xong (chu kỳ HUB_SORTED_CHECK_HOURS)
# đơn đã chuyển khỏi Đã đặt mà hành trình lưu chưa kết thúc (vd đang giao thì bấm Thanh toán): tra lại mỗi chừng này
# giờ chỉ để cập nhật bảng track_logs (không alert), tới khi hãng báo đã giao / hoàn xong
UNFINISHED_LOG_HOURS = 3
# đơn mới có mã vận đơn (chưa có giờ tra): tra lần đầu ngẫu nhiên trong chừng này phút (trước đây: trong 12 giờ →
# đơn đã tới bưu cục giao vẫn chưa được xếp "sắp giao", vd #76 11/2026)
FIRST_CHECK_MINUTES = 30
# Khoảng cách tối thiểu (giây) giữa 2 lần gọi API của cùng 1 hãng, dùng chung cho bot + web (khe giờ lưu trong
# bảng meta, db.reserve_slot) → watcher và nút tra trên web chạy cùng lúc vẫn không dồn API hãng (GHN 429, SPX)
API_MIN_GAP_SECONDS: dict[str, float] = {"spx": 2.0, "jt": 2.0, "ghn": 2.0}
NEAR_WHEN_ETA_DUE = True
NEAR_ETA_DAYS = 1

ALERT_KEYWORDS: dict[str, list[str]] = {
    "*": [
        *ALERT_PLACES,   # .env VC_ALERT_PLACES
    ],
    "SPX": [
        "Đang giao hàng",
    ],
    "J&T": [
        "đang giao hàng",
    ],
    "GHN": [
        "Đang giao hàng",
    ],
}

NEAR_KEYWORDS: dict[str, list[str]] = {
    "*": [],
    "SPX": [],
    "J&T": [
        # sắp giao = hàng ĐÃ TỚI bưu cục giao của tỉnh nơi nhận (người dùng yêu cầu, 11/2026), mọi bưu cục
        # "(<mã tỉnh>) …": "Hàng đã được chuyển đến (<mã tỉnh>) <bưu cục>" (mã tỉnh = .env VC_JT_AREA)
        # → tra mỗi NEAR_CHECK_HOURS giờ tới khi "Nhân viên … đang giao hàng" (ALERT_KEYWORDS).
        # vd đơn J&T: tới bưu cục giao 05:37 → đi giao 07:29
        # Còn "… đang chuyển hàng đến (<mã tỉnh>) …" thì CHƯA là sắp giao (xem HUB_SORTED_KEYWORDS).
        *[f"Hàng đã được chuyển đến ({a})" for a in JT_AREAS],
    ],
    "GHN": [
        "bưu cục giao",     # "Sẵn sàng xuất đến bưu cục giao" (~15 giờ trước khi giao), "Nhập hàng vào bưu cục giao"
    ],
}

# mốc khớp NEAR_KEYWORDS mà có cụm này thì không tính (chỉ áp cho NEAR_KEYWORDS, không cho ALERT_KEYWORDS):
# J&T "Hàng đã được chuyển đến (<mã tỉnh>) HUB <tên>" là kho trung chuyển, không phải bưu cục giao
NEAR_EXCLUDE_KEYWORDS: dict[str, list[str]] = {
    "*": [],
    "SPX": [],
    "J&T": ["HUB"],
    "GHN": [],
}

# kho cuối trước bưu cục giao (tên kho trong .env, lấy từ hành trình các đơn đã giao). Ghi cụm "đã đến kho …" chứ
# không chỉ tên kho: tên kho xuất hiện sớm hơn nhiều trong mốc "đang trung chuyển tới …".
# vd: tới kho cuối 14:04 → kho / bưu cục giao sáng hôm sau 07:05 → giao 08:06
HUB_KEYWORDS: dict[str, list[str]] = {
    "*": [],
    "SPX": [f"đã đến kho {h}" for h in HUB_SPX],          # .env VC_HUB_SPX (kho phân loại "… Mega SOC")
    "J&T": [f"đã được chuyển đến {h}" for h in HUB_JT],   # .env VC_HUB_JT
    "GHN": [f"lưu tại {h}" for h in HUB_GHN],             # .env VC_HUB_GHN
}

# sau mốc tới kho cuối (HUB_KEYWORDS) mà có mốc này → đã phân loại xong, sắp lên xe đi kho giao: tra dày hơn.
# vd SPX: tới kho cuối 14:04, phân loại 20:01, lên xe 01:37 hôm sau
HUB_SORTED_KEYWORDS: dict[str, list[str]] = {
    "*": [],
    "SPX": ["đã được phân loại và sẵn sàng trung chuyển đến điểm kế tiếp"],
    # rời kho cuối, đang trên đường tới bưu cục giao (chưa phải sắp giao): "Bưu cục <kho cuối> đang chuyển hàng
    # đến (<mã tỉnh>) <bưu cục>"
    "J&T": [f"đang chuyển hàng đến ({a})" for a in JT_AREAS],
    "GHN": [],
}

# trạng thái đã giao xong → thôi tra dày (vẫn tra 12 giờ/lần cho tới khi đơn đổi trạng thái)
DONE_MILESTONES = ["Đã giao hàng", "Giao hàng thành công"]
# đơn bị hủy / hoàn về người bán: luôn gửi alert (không cần khớp ALERT_KEYWORDS), bot chính gửi kèm đơn
# với nút chuyển sang Đã hủy. Vd SPX đơn #19: "Đơn vị vận chuyển xác nhận đơn hàng Huỷ thành công",
# "Đang trả hàng", "Trả hàng thành công".
CANCEL_KEYWORDS: dict[str, list[str]] = {
    "*": ["hủy thành công", "đã hủy", "trả hàng thành công", "đang trả hàng", "chuyển hoàn", "hoàn hàng"],
    "SPX": [],
    "J&T": [],
    "GHN": [],
}
# hành trình đã kết thúc (cùng DONE_MILESTONES): khớp trạng thái hiện tại hoặc mốc mới nhất thì lưu hẳn vào bảng
# track_logs, lần sau xem lại không gọi API hãng nữa. Chỉ ghi trạng thái cuối cùng: "đang trả hàng", "giao không
# thành công / hẹn giao lại" chưa phải kết thúc.
FINAL_KEYWORDS: dict[str, list[str]] = {
    "*": ["trả hàng thành công", "hủy thành công", "hoàn hàng thành công", "đã hoàn hàng", "đã trả hàng", "đã bị hủy"],
    "SPX": ["Cancelled"],   # #21: trạng thái "Cancelled", mốc "Đơn vị vận chuyển thông báo đơn hàng đã bị hủy"
    "J&T": [],
    "GHN": [],
}
# cụm từ cho biết giao lỗi (gắn ⚠️ vào tiêu đề alert và mốc mới nhất trong /danggiao)
# J&T: "(<mã tỉnh>)<bưu cục> Đã đăng ký Kiện Vấn Đề … Không liên lạc được Khách hàng/khóa máy"
FAIL_KEYWORDS = ["không thành công", "thất bại", "giao lại", "hoàn hàng", "chuyển hoàn", "có vấn đề", "kiện vấn đề"]

# /danggiao: MỐC MỚI NHẤT khớp 1 cụm dưới đây → nhóm "🏍 Đang giao" (đã có nhân viên nhận đi giao, đang trên đường);
# còn lại (đã tới bưu cục giao cuối, chưa đi giao / giao lỗi chờ giao lại) → nhóm "📮 Sắp giao".
# J&T ghi tên + SĐT nhân viên: "Nhân viên … của bưu cục (<mã tỉnh>) <bưu cục> đang giao hàng. +849…"
OUT_FOR_DELIVERY_KEYWORDS: dict[str, list[str]] = {
    "*": [],
    "SPX": ["Đã sắp xếp tài xế giao hàng", "Đang giao hàng"],
    "J&T": ["đang giao hàng"],
    "GHN": ["Đang giao hàng"],   # "Đang giao hàng: NV giao hàng đang trên đường…", "Đang giao hàng lần 2: …"
}


def _for(table: dict[str, list[str]], carrier: str) -> list[str]:
    return table.get("*", []) + table.get(carrier, [])


def keywords_for(carrier: str) -> list[str]:
    return _for(ALERT_KEYWORDS, carrier)


def cancel_keywords_for(carrier: str) -> list[str]:
    return _for(CANCEL_KEYWORDS, carrier)


def near_keywords_for(carrier: str) -> list[str]:
    """Chỉ NEAR_KEYWORDS (sắp giao còn gồm ALERT_KEYWORDS, xem bot.delivery_level)."""
    return _for(NEAR_KEYWORDS, carrier)


def near_exclude_for(carrier: str) -> list[str]:
    return _for(NEAR_EXCLUDE_KEYWORDS, carrier)


def final_keywords_for(carrier: str) -> list[str]:
    return _for(FINAL_KEYWORDS, carrier)


def hub_keywords_for(carrier: str) -> list[str]:
    return _for(HUB_KEYWORDS, carrier)


def out_for_delivery_keywords_for(carrier: str) -> list[str]:
    return _for(OUT_FOR_DELIVERY_KEYWORDS, carrier)


def hub_sorted_keywords_for(carrier: str) -> list[str]:
    return _for(HUB_SORTED_KEYWORDS, carrier)
