import contextvars
import decimal
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import rpc
import sale_text
from utils import name_key, norm_phone, price_after_discount  # noqa: F401 – bot gọi db.name_key / db.norm_phone

log = logging.getLogger("db")

# thư mục file của web: bản chép ảnh QR theo tên người đặt (cho dễ xem trên máy). Bản gốc mọi ảnh nằm trong
# database (bảng image_files). Docker: /app/data (volume ./data); chạy tại chỗ: web/data; Vercel: /tmp/….
# Vercel (VERCEL=1): đĩa chỉ đọc, chỉ ghi được /tmp
FILES_DIR = os.getenv("FILES_DIR") or ("/tmp/quan-li-don" if os.getenv("VERCEL") else
                                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
# trước 11/2026 ảnh tải về nằm ở đĩa (data/qr_cache, tên = băm file_id): chỉ còn đọc để chép vào database
LEGACY_CACHE_DIR = os.path.join(FILES_DIR, "qr_cache")


def legacy_cache_path(file_id: str) -> str:
    return os.path.join(LEGACY_CACHE_DIR, hashlib.sha256(file_id.encode()).hexdigest()[:32] + ".jpg")


# ảnh QR đặt tên dễ đọc theo người đặt: data/images/qr/<tên>.jpg (tự đồng bộ, xem sync_qr_images)
QR_IMAGES_DIR = os.path.join(FILES_DIR, "images", "qr")
QR_IMAGES_INDEX = ".file_ids.json"  # {tên file: file_id đã chép} → chỉ đọc ảnh từ database khi QR đổi


def sync_qr_images() -> int:
    """Chép QR (ảnh trong database) ra QR_IMAGES_DIR theo tên người đặt; xóa file của người không còn / hết QR.
    Gọi lại thoải mái (chỉ đọc + ghi file của QR mới / đã đổi). Trả về số file đang có."""
    os.makedirs(QR_IMAGES_DIR, exist_ok=True)
    index_path = os.path.join(QR_IMAGES_DIR, QR_IMAGES_INDEX)
    try:
        with open(index_path, encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        done = {}
    with connect() as conn:
        people = conn.execute("SELECT n.ten, n.qr_file_id FROM nguoi_dat n "
                              "JOIN image_files i ON i.file_id = n.qr_file_id").fetchall()
    wanted = {re.sub(r'[\\/:*?"<>|]+', "_", " ".join(p["ten"].split())) + ".jpg": p["qr_file_id"] for p in people}
    for name, file_id in wanted.items():
        dst = os.path.join(QR_IMAGES_DIR, name)
        if done.get(name) != file_id or not os.path.exists(dst):
            if (data := get_image(file_id)) is not None:
                with open(dst + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(dst + ".tmp", dst)
    for name in os.listdir(QR_IMAGES_DIR):
        if name not in wanted and name != QR_IMAGES_INDEX:
            os.remove(os.path.join(QR_IMAGES_DIR, name))
    have = {name: fid for name, fid in wanted.items() if os.path.exists(os.path.join(QR_IMAGES_DIR, name))}
    if have != done:
        with open(index_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(have, f, ensure_ascii=False)
        os.replace(index_path + ".tmp", index_path)
    return len(have)


# ---------- ảnh (QR trả công, ảnh sản phẩm): bản gốc lưu trong database ----------
# Đơn / người đặt / ảnh sản phẩm giữ file_id Telegram (bot gửi lại ảnh bằng file_id cho nhanh); bảng image_files giữ
# chính file ảnh theo file_id → web hiện ảnh không cần Telegram, sao lưu database là có cả ảnh, đổi bot / file_id
# hỏng vẫn còn ảnh (bot gửi bằng dữ liệu ảnh rồi đổi sang file_id mới: replace_file_id).
IMAGE_KEEP_DAYS = 1  # ảnh không còn ai dùng (đổi QR, xóa người, xóa ảnh sản phẩm…) giữ thêm chừng này ngày rồi xóa
_IMAGE_REFS = ("SELECT qr_file_id AS f FROM nguoi_dat UNION SELECT qr_file_id FROM orders "
               "UNION SELECT file_id FROM product_images")


def save_image(file_id: str, data: bytes):
    """Lưu ảnh (bot lưu ngay khi nhận ảnh QR / ảnh sản phẩm, web khi tải lên / tải từ Telegram)."""
    with connect() as conn:
        conn.execute("INSERT INTO image_files (file_id, data, created_at) VALUES (?, ?, ?) ON CONFLICT (file_id) "
                     "DO UPDATE SET data = excluded.data, created_at = excluded.created_at", (file_id, data, _now()))


def get_image(file_id: str) -> bytes | None:
    with connect() as conn:
        r = conn.execute("SELECT data FROM image_files WHERE file_id = ?", (file_id,)).fetchone()
    return r[0] if r else None


def missing_images() -> list[str]:
    """file_id đang dùng (QR người đặt / đơn, ảnh sản phẩm) mà database chưa có ảnh."""
    with connect() as conn:
        return [r[0] for r in conn.execute(
            f"SELECT f FROM ({_IMAGE_REFS}) u WHERE f IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM image_files i WHERE i.file_id = u.f) ORDER BY f")]


def prune_images() -> int:
    """Xóa ảnh không còn ai dùng, lưu đã quá IMAGE_KEEP_DAYS ngày (ảnh vừa nhận trong lúc /them chưa xong
    thì chưa gắn vào đơn nào – giữ lại). Trả về số ảnh đã xóa."""
    cutoff = (datetime.now() - timedelta(days=IMAGE_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        return conn.execute(
            f"DELETE FROM image_files WHERE created_at < ? AND file_id NOT IN "
            f"(SELECT f FROM ({_IMAGE_REFS}) u WHERE f IS NOT NULL)", (cutoff,)).rowcount


def replace_file_id(old: str, new: str):
    """file_id cũ hỏng (đổi bot…), bot đã gửi lại ảnh bằng dữ liệu trong database và Telegram cấp file_id mới →
    đổi mọi chỗ dùng sang file_id mới (ảnh trong database giữ nguyên)."""
    if not new or new == old:
        return
    with connect() as conn:
        conn.execute("UPDATE nguoi_dat SET qr_file_id = ? WHERE qr_file_id = ?", (new, old))
        conn.execute("UPDATE orders SET qr_file_id = ? WHERE qr_file_id = ?", (new, old))
        conn.execute("UPDATE product_images SET file_id = ? WHERE file_id = ?", (new, old))
        if conn.execute("SELECT 1 FROM image_files WHERE file_id = ?", (new,)).fetchone():
            conn.execute("DELETE FROM image_files WHERE file_id = ?", (old,))
        else:
            conn.execute("UPDATE image_files SET file_id = ? WHERE file_id = ?", (new, old))

# Các trường của từng bảng: (tên cột, nhãn hiển thị)
ORDER_FIELDS = [
    ("ten_nguoi_dat", "Tên người đặt"),
    ("ten_tren_don", "Tên trên đơn"),  # tên người đặt ghi trên đơn Shopee / TikTok (có thể khác tên quen gọi)
    ("chien_dich", "Chiến dịch thưởng"),  # id campaigns (thưởng săn sale), NULL = không thuộc chiến dịch nào
    ("nguon_khach", "Nguồn khách"),
    ("ten_sp", "Tên sản phẩm"),
    ("so_luong", "Số lượng"),
    ("mau", "Màu / phân loại"),  # mô tả chi tiết từng đơn (người dùng yêu cầu 11/2026): màu, size, phiên bản…
    ("mo_ta", "Mô tả chi tiết"),  # cấu hình, serial, ghi chú về món hàng (nhiều dòng)
    ("bh_kich", "Ngày kích bảo hành"),  # YYYY-MM-DD
    ("bh_thang", "Bảo hành (tháng)"),  # số tháng bảo hành → hết hạn = bh_kich + bh_thang tháng
    ("nguon_don", "Nguồn đơn"),
    ("shop", "Shop"),  # shop đã mua (tên khớp sổ shop `shops` → biết shop có xuất VAT không)
    ("gia_dat", "Giá đặt"),
    ("gia_thu", "Giá shop thu"),
    ("bonus_thu", "Bonus thu"),  # tiền thu thêm cả đơn (không nhân số lượng), cộng vào thu khi tính lãi (order_thu)
    ("cong_dat", "Công đặt"),
    ("sdt", "SĐT"),
    ("dia_chi", "Địa chỉ"),
    ("ma_don", "Mã đơn hàng"),
    ("don_vi_vc", "Đơn vị vận chuyển"),
    ("trang_thai", "Trạng thái"),
]
# Luồng đơn: Đã đặt → Đã nhận (đã nhận hàng, chưa trả công) → Thanh toán (đã nhận hàng + trả công người đặt)
# → Hoàn thành (đã bán, có giá shop thu)
STATUS_ORDERED, STATUS_RECEIVED, STATUS_PAID = "Đã đặt", "Đã nhận", "Thanh toán"
STATUS_DONE, STATUS_CANCELED = "Hoàn thành", "Đã hủy"
STATUSES = [STATUS_ORDERED, STATUS_RECEIVED, STATUS_PAID, STATUS_DONE, STATUS_CANCELED]
UNPAID_STATUSES = (STATUS_ORDERED, STATUS_RECEIVED)  # công chưa trả → trả công chuyển sang Thanh toán
UNSOLD_STATUSES = (STATUS_ORDERED, STATUS_RECEIVED, STATUS_PAID)  # chưa bán, còn là vốn
STATUS_FLOW = [STATUS_ORDERED, STATUS_RECEIVED, STATUS_PAID, STATUS_DONE]


def status_change_error(old: str | None, new: str) -> str | None:
    """Luật đổi trạng thái (web, người dùng yêu cầu): đi tới từng bước Đã đặt → Đã nhận → Thanh toán → Hoàn thành,
    không nhảy cóc; lùi về bước trước thì được (sửa nhầm). Hủy được khi chưa Hoàn thành; đơn hủy chỉ khôi phục về
    Đã đặt. Trả về câu báo lỗi, None = được đổi."""
    old = old or STATUS_ORDERED
    if new == old:
        return None
    if new == STATUS_CANCELED:
        return "Đơn đã Hoàn thành, không hủy được (xóa giá shop thu để lùi về Thanh toán trước)" \
            if old == STATUS_DONE else None
    if old == STATUS_CANCELED:
        return None if new == STATUS_ORDERED else "Đơn đã hủy chỉ khôi phục về Đã đặt"
    if new not in STATUS_FLOW or old not in STATUS_FLOW:
        return None
    i, j = STATUS_FLOW.index(old), STATUS_FLOW.index(new)
    if j > i + 1:
        return f"Không nhảy cóc: đơn {old} phải chuyển sang {STATUS_FLOW[i + 1]} trước"
    return None


def next_statuses(old: str | None) -> list[str]:
    """Trạng thái đơn được chuyển tới từ old (kể cả giữ nguyên), theo thứ tự STATUSES."""
    return [st for st in STATUSES if not status_change_error(old, st)]
# tên trạng thái cũ → mới
_OLD_STATUSES = {"Đang đặt": STATUS_ORDERED, "Đã giao": STATUS_PAID}
SALE_FIELDS = [
    ("ten_sp", "Tên sản phẩm"),
    ("shop", "Shop"),  # 1 sản phẩm có thể có nhiều shop: mỗi shop 1 dòng sales (giá / link riêng)
    ("gia_goc", "Giá gốc"),
    ("gia_sale", "Giá mua được"),
    ("gia_sale_max", "Giá mua được tối đa"),  # có khi giá là khoảng: 4tr-4tr2
    ("giam_pt", "Giảm giá (%)"),  # có thì giá mua được = giá gốc giảm % (tự tính, _sale_derive)
    ("giam_toi_da", "Giảm tối đa"),  # số tiền giảm tối đa khi tính theo % (vd 35% tối đa 2tr5), 0 = không giới hạn
    ("cong", "Công"),
    ("cong_max", "Công tối đa"),
    ("so_luong", "Số lượng"),
    ("uu_tien", "Thứ tự ưu tiên"),  # 1, 2, 3… = vị trí trong danh sách sale (1 lên đầu), 0 = không xếp → sau cùng
    ("nhom", "Nhóm sale"),  # 1, 2, 3… → /sale 1 chỉ hiện nhóm 1; 0 / NULL = chưa chia nhóm
    ("muc_gia", "Mức giá khác"),  # mỗi dòng 1 mức "dưới 4tr5 công 100k" (sale_text.parse_tiers)
    ("link", "Link"),
    ("active", "Active"),
]
PRODUCT_FIELDS = [
    ("ten", "Tên sản phẩm"),
    ("gia_goi_y", "Giá thu gợi ý"),
    ("gia_goi_y_max", "Giá thu gợi ý tối đa"),
]
# danh mục phụ: chỉ sửa qua make_edit (/suand, /suadvvc, /suasdt), không có luồng /them
PERSON_FIELDS = [("ten", "Tên"), ("sdt", "SĐT"), ("zalo", "Zalo"), ("facebook", "Facebook"), ("ha_noi", "Ở Hà Nội"),
                 ("ghi_chu", "Ghi chú")]
CARRIER_FIELDS = [("ten", "Tên")]
ADDRESS_FIELDS = [("noi_dung", "Địa chỉ")]  # sổ địa chỉ đặt hàng: chỉ 1 ô văn bản tự do (nhiều dòng)
PHONE_FIELDS = [("cua_toi", "Chủ số"), ("may", "Lắp ở máy"), ("ghi_chu", "Ghi chú"), ("da_su_dung", "Trạng thái")]
TABLES = {"orders": ORDER_FIELDS, "sales": SALE_FIELDS, "products": PRODUCT_FIELDS,
          "nguoi_dat": PERSON_FIELDS, "carriers": CARRIER_FIELDS, "phones": PHONE_FIELDS,
          "addresses": ADDRESS_FIELDS}
# cột lưu được khi insert nhưng không hiện / sửa như trường thường
EXTRA_COLUMNS = {"orders": ["qr_file_id"], "sales": [], "products": [], "addresses": []}  # chỉ bảng có db.insert
MONEY_FIELDS = {"gia_dat", "gia_thu", "bonus_thu", "cong_dat", "gia_goc", "giam_toi_da", "gia_sale", "gia_sale_max", "cong", "cong_max",
                "gia_goi_y", "gia_goi_y_max"}
# trường tiền nhận khoảng giá → cột lưu giá trị lớn
RANGE_FIELDS = {"gia_sale": "gia_sale_max", "cong": "cong_max", "gia_goi_y": "gia_goi_y_max"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ten_nguoi_dat TEXT NOT NULL,
    ten_tren_don  TEXT,             -- tên ghi trên đơn hàng (người nhận trên sàn)
    chien_dich    INTEGER,          -- campaigns.id (thưởng săn sale), NULL = không thuộc chiến dịch
    nguon_khach   TEXT,             -- Zalo / Facebook
    ten_sp        TEXT,
    so_luong      INTEGER,          -- NULL = không ghi
    mau           TEXT,             -- màu / phân loại (size, phiên bản…)
    mo_ta         TEXT,             -- mô tả chi tiết món hàng
    bh_kich       TEXT,             -- ngày kích hoạt bảo hành YYYY-MM-DD
    bh_thang      INTEGER,          -- số tháng bảo hành
    nguon_don     TEXT,             -- Shopee / TikTok
    shop          TEXT,             -- shop đã mua (khớp sổ shop theo name_key)
    gia_dat       INTEGER DEFAULT 0,
    gia_thu       INTEGER DEFAULT 0,
    bonus_thu     INTEGER DEFAULT 0,  -- thu thêm (cả đơn), cộng vào giá shop thu khi tính lãi
    cong_dat      INTEGER DEFAULT 0,
    sdt           TEXT,
    dia_chi       TEXT,
    ma_don        TEXT,
    don_vi_vc     TEXT,
    qr_file_id    TEXT,             -- file_id Telegram của ảnh QR trả công người đặt
    vc_alert_last TEXT,             -- mốc hành trình mới nhất đã gửi alert (tránh báo lặp)
    vc_near       INTEGER NOT NULL DEFAULT 0,  -- 1 = sắp giao (NEAR_CHECK_HOURS), 2 = đã về kho cuối (HUB_CHECK_HOURS), 3 = kho cuối đã phân loại (HUB_SORTED_CHECK_HOURS)
    vc_next_check REAL,             -- giờ (timestamp) tra hành trình lần tới của đơn này
    trang_thai    TEXT NOT NULL DEFAULT 'Đã đặt',  -- Đã đặt / Đã nhận / Thanh toán / Hoàn thành / Đã hủy
    created_by    INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_sdt ON orders(sdt);
CREATE INDEX IF NOT EXISTS idx_orders_ma_don ON orders(ma_don);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);

-- giá trị lặt vặt của bot (vd: lần kiểm tra vận chuyển gần nhất)
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- chiến dịch thưởng săn sale: mỗi người đặt cứ moc_don đơn (Thanh toán / Hoàn thành) gán vào chiến dịch
-- (orders.chien_dich) được thưởng thuong (campaign_stats)
CREATE TABLE IF NOT EXISTS campaigns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ten        TEXT NOT NULL,
    tu         TEXT NOT NULL,                -- YYYY-MM-DD: thời gian chạy (đang chạy / đã kết thúc, gợi ý khi chọn)
    den        TEXT,                         -- YYYY-MM-DD; NULL = chưa đặt ngày kết thúc
    moc_don    INTEGER NOT NULL,             -- cứ N đơn gán vào chiến dịch …
    thuong     INTEGER NOT NULL,             -- … được thưởng số tiền này (VND)
    active     INTEGER NOT NULL DEFAULT 1,   -- 1 = đang bật: hiện để chọn cho đơn, /thuongsale mặc định; 0 = tắt
    created_at TEXT NOT NULL
);

-- hành trình vận chuyển lần tra gần nhất theo mã vận đơn (JSON tracking.result_to_dict); final = 1 khi đã giao xong /
-- hoàn xong → tracking.track_saved trả bản lưu, không gọi API hãng nữa
CREATE TABLE IF NOT EXISTS track_logs (
    ma_don      TEXT PRIMARY KEY,   -- mã vận đơn viết hoa, bỏ khoảng trắng
    carrier     TEXT,
    milestone   TEXT,
    data        TEXT NOT NULL,
    final       INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);

-- nhật ký đổi trạng thái đơn (người dùng yêu cầu): mỗi lần trang_thai đổi qua db.update → 1 dòng tu → den, giờ đổi,
-- nguon (bot / web), nguoi (tên Telegram / tài khoản web). Mốc tạo đơn (Đã đặt) = orders.created_at, không ghi ở đây.
CREATE TABLE IF NOT EXISTS order_status_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    tu          TEXT,
    den         TEXT NOT NULL,
    at          TEXT NOT NULL,
    nguon       TEXT,
    nguoi       TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_log_order ON order_status_log(order_id);

-- người nhận tin từ bot alert (không dùng được bot chính – chỉ admin trong .env)
-- trạng thái: cho_duyet (vừa bấm Start bot alert) → da_duyet (nhận alert) | tu_choi
CREATE TABLE IF NOT EXISTS alert_users (
    chat_id     INTEGER PRIMARY KEY,
    ten         TEXT,
    username    TEXT,
    trang_thai  TEXT NOT NULL DEFAULT 'da_duyet',
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);

-- các bot gửi alert admin nhập ở web (tab 🤖 Bot alert); alert_users.bot_id chọn bot gửi cho từng người
-- (NULL = bot mặc định trong .env: ALERT_BOT_TOKEN, trống thì BOT_TOKEN)
CREATE TABLE IF NOT EXISTS alert_bots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,
    ten         TEXT,
    username    TEXT,
    mac_dinh    INTEGER NOT NULL DEFAULT 0,  -- 1 = bot mặc định (người nhận chưa chọn bot + admin trong .env)
    created_at  TEXT NOT NULL
);

-- người đặt hộ (orders.ten_nguoi_dat): lưu 1 ảnh QR trả công dùng chung cho mọi đơn của người đó
CREATE TABLE IF NOT EXISTS nguoi_dat (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ten         TEXT NOT NULL,
    ten_key     TEXT NOT NULL UNIQUE,   -- name_key(ten): chữ thường, gộp khoảng trắng
    sdt         TEXT,
    qr_file_id  TEXT,
    zalo        TEXT,                   -- link Zalo (utils.social_link)
    facebook    TEXT,                   -- link Facebook
    ha_noi      INTEGER,                -- 1 = ở Hà Nội, 0 = không, NULL = chưa rõ
    ghi_chu     TEXT,
    created_at  TEXT NOT NULL
);

-- danh bạ SĐT dùng để đặt đơn (orders.sdt lưu nguyên văn; so khớp theo số đã chuẩn hóa)
CREATE TABLE IF NOT EXISTS phones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sdt         TEXT NOT NULL UNIQUE,   -- chuẩn hóa: chỉ chữ số, +84/84 → 0
    ghi_chu     TEXT,
    da_su_dung  INTEGER NOT NULL DEFAULT 0,  -- 1 = đang dùng (có đơn chưa xong), 0 = free; tự cập nhật theo trạng thái đơn
    cua_toi     INTEGER NOT NULL DEFAULT 0,  -- 1 = số của mình (chủ shop), 0 = số người khác
    may         TEXT,                        -- sim đang lắp ở điện thoại nào (ghi tự do)
    created_at  TEXT NOT NULL
);

-- danh sách đơn vị vận chuyển để chọn khi tạo/sửa đơn (orders.don_vi_vc lưu tên)
CREATE TABLE IF NOT EXISTS carriers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ten         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at  TEXT NOT NULL
);

-- danh mục sản phẩm: tên chuẩn để chọn khi thêm đơn / săn sale + giá shop thu gợi ý (nhập tay)
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ten         TEXT NOT NULL,
    ten_key     TEXT NOT NULL UNIQUE,   -- name_key(ten)
    gia_goi_y     INTEGER NOT NULL DEFAULT 0,  -- giá shop thu gợi ý; 0 = chưa có
    gia_goi_y_max INTEGER,                     -- NULL nếu không phải khoảng giá
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);

-- tài khoản đăng nhập web (web.py); tạo admin đầu tiên: python manage.py createadmin <tên>
CREATE TABLE IF NOT EXISTS web_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,            -- scrypt$<salt hex>$<hash hex>
    role          TEXT NOT NULL DEFAULT 'user',  -- admin (quản lí tài khoản) / user
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login    TEXT
);
-- ảnh sản phẩm (bot /anhsp, web tab Sản phẩm): gắn theo tên (name_key) → món sale cùng tên dùng chung, /sale gửi kèm
CREATE TABLE IF NOT EXISTS product_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ten_key     TEXT NOT NULL,          -- name_key(tên sản phẩm / ten_sp của món sale)
    file_id     TEXT NOT NULL,          -- file_id Telegram của bot chính (ảnh lưu ở image_files như ảnh QR)
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_images_key ON product_images(ten_key);

-- ảnh gốc theo file_id Telegram (QR trả công, ảnh sản phẩm): web hiện ảnh từ đây, không cần Telegram (save_image)
CREATE TABLE IF NOT EXISTS image_files (
    file_id     TEXT PRIMARY KEY,
    data        BLOB NOT NULL,          -- file ảnh (JPEG)
    created_at  TEXT NOT NULL           -- lúc lưu (prune_images giữ ảnh chưa ai dùng ít nhất IMAGE_KEEP_DAYS)
);

-- sổ shop (người dùng yêu cầu 11/2026): shop có xuất hóa đơn VAT không, để sau chỉ mua ở shop xuất VAT.
-- Đơn (orders.shop) và săn sale (sales.shop) ghi tên shop; khớp sổ theo name_key(tên)
CREATE TABLE IF NOT EXISTS shops (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ten         TEXT NOT NULL,
    ten_key     TEXT NOT NULL UNIQUE,      -- name_key(ten)
    vat         INTEGER,                   -- 1 = có xuất VAT, 0 = không, NULL = chưa rõ
    ghi_chu     TEXT,
    updated_at  TEXT NOT NULL
);

-- sổ địa chỉ kho / bưu cục của hãng vận chuyển (web tab 🏍 Giao hàng): tên đúng như trong hành trình
-- (vd "20-HNI Abc 1 Hub") → địa chỉ thật + ghi chú; hành trình có mốc nhắc tới tên kho thì hiện kèm địa chỉ
CREATE TABLE IF NOT EXISTS warehouses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ten         TEXT NOT NULL,
    ten_key     TEXT NOT NULL UNIQUE,      -- name_key(ten): không trùng tên (hoa / thường, khoảng trắng)
    dia_chi     TEXT,
    ghi_chu     TEXT,
    updated_at  TEXT NOT NULL
);

-- sổ địa chỉ đặt hàng (bot /diachi, web tab 📍 Địa chỉ): mỗi dòng 1 ô văn bản tự do
CREATE TABLE IF NOT EXISTS addresses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    noi_dung    TEXT NOT NULL,
    created_by  INTEGER,
    created_at  TEXT NOT NULL
);

-- phiên đăng nhập web: cookie giữ token, DB chỉ lưu sha256(token)
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ten_sp      TEXT NOT NULL,
    shop        TEXT,
    gia_sale    INTEGER DEFAULT 0,
    gia_sale_max INTEGER,            -- NULL nếu không phải khoảng giá
    cong        INTEGER DEFAULT 0,
    cong_max    INTEGER,
    so_luong    INTEGER,             -- NULL = không rõ
    muc_gia     TEXT,                -- mức giá khác, mỗi dòng "dưới 4tr5 công 100k"
    gia_goc     INTEGER,             -- giá gốc (trước giảm)
    giam_pt     REAL,                -- % giảm nhập tay; có thì gia_sale = gia_goc giảm % (NULL = nhập thẳng giá)
    giam_toi_da INTEGER,             -- số tiền giảm tối đa của % giảm (0 / NULL = không giới hạn)
    uu_tien     INTEGER NOT NULL DEFAULT 0,  -- thứ tự ưu tiên 1, 2, 3… (1 lên đầu), 0 = sau cùng (list_sales)
    nhom        INTEGER DEFAULT 0,   -- nhóm sale 1, 2… (/sale 1); 0 / NULL = chưa chia nhóm
    link        TEXT,                -- nhiều link cách nhau xuống dòng
    ton_kho     TEXT,                -- JSON {link: kết quả kiểm tra còn hàng} do tiện ích Chrome gửi về (set_sale_stock)
    phan_loai   TEXT,                -- JSON {link: [tên phân loại cần mua]} bấm 🎯 trên web (set_sale_variants)
    active      INTEGER NOT NULL DEFAULT 1,  -- 1 = còn chương trình sale
    created_by  INTEGER,
    created_at  TEXT NOT NULL
);
"""


# ---------- Postgres (DATABASE_URL, bắt buộc – từ 11/2026 không còn SQLite) ----------
# Câu SQL trong file viết kiểu cũ (dấu ?, kiểu INTEGER / REAL / TEXT, INSERT … ON CONFLICT, RETURNING); lớp _PgConn
# đổi sang Postgres: ? / :tên → %s / %(tên)s, LIKE → ILIKE (không phân biệt hoa thường), kiểu cột (INTEGER → BIGINT,
# REAL → DOUBLE PRECISION, BLOB → BYTEA, TEXT → TEXT COLLATE "C" = so / sắp theo byte), PRAGMA table_info → information_schema,
# kết quả Decimal / bool → int / float.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# name_key() trong Postgres: hàm SQL, không phụ thuộc locale của database (lower() ở locale C chỉ đổi A-Z) →
# lower() + translate() từng chữ hoa tiếng Việt, mọi khoảng trắng (str.isspace như str.split) → 1 dấu cách, trim.
_VI_UPPER = "ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ"
_SPACE_CHARS = "".join(chr(c) for c in range(0x3001) if chr(c).isspace())
PG_NAME_KEY_FUNC = (
    "CREATE OR REPLACE FUNCTION name_key(t text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$ "
    f"SELECT btrim(regexp_replace(translate(lower(coalesce(t, '')), '{_VI_UPPER}', '{_VI_UPPER.lower()}'), "
    f"'[{_SPACE_CHARS}]+', ' ', 'g'), ' ') $f$")

_PG_DDL = re.compile(r"^\s*(CREATE\s+TABLE|ALTER\s+TABLE)\b", re.I)
_PG_TYPE_RULES = [
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY(\s+AUTOINCREMENT)?\b", re.I), "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    (re.compile(r"\bINTEGER\b", re.I), "BIGINT"),
    (re.compile(r"\bREAL\b", re.I), "DOUBLE PRECISION"),
    (re.compile(r"\bBLOB\b", re.I), "BYTEA"),
]
_PG_TEXT = re.compile(r"\bTEXT\b(?!\s+COLLATE)", re.I)
_PG_NOCASE = re.compile(r"\s+COLLATE\s+NOCASE\b", re.I)
# cột ghi UNIQUE COLLATE NOCASE trong SCHEMA → chỉ mục duy nhất theo lower(cột) (không trùng khi khác hoa / thường)
PG_NOCASE_UNIQUE = {"carriers": "ten", "web_users": "username"}
_PG_PRAGMA_COLS = re.compile(r"^\s*PRAGMA\s+table_info\(\s*(\w+)\s*\)\s*$", re.I)
_PG_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_PG_LIKE = re.compile(r"\bLIKE\b", re.I)
_pg_sql_cache: dict[tuple[str, bool], str] = {}


def _pg_sql(sql: str, named: bool) -> str:
    key = (sql, named)
    if (out := _pg_sql_cache.get(key)) is None:
        out = sql
        for rx, rep in _PG_TYPE_RULES:
            out = rx.sub(rep, out)
        if _PG_DDL.match(out):
            out = _PG_TEXT.sub('TEXT COLLATE "C"', _PG_NOCASE.sub("", out))
        out = _PG_LIKE.sub("ILIKE", out).replace("%", "%%")
        out = _PG_NAMED.sub(r"%(\1)s", out) if named else out.replace("?", "%s")
        _pg_sql_cache[key] = out
    return out


def _pg_value(v):
    """Kết quả Postgres → kiểu Python code dùng: Decimal (SUM / AVG) → int / float, bool → 0/1, memoryview → bytes."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, memoryview):
        return bytes(v)
    return v


class _PgCursor:
    """Kết quả psycopg → dòng truy cập theo tên cột (rpc.RemoteRow: r["cột"], r[0], keys()), duyệt bằng for được."""

    def __init__(self, cur, rows=None, cols=None):
        self._cur = cur
        self._rows, self._cols = rows, cols  # kết quả dựng sẵn (PRAGMA)

    def _names(self):
        if self._cols is not None:
            return self._cols
        return [d.name for d in self._cur.description] if self._cur is not None and self._cur.description else []

    def _wrap(self, r, cols):
        return rpc.RemoteRow(cols, [_pg_value(v) for v in r])

    def fetchone(self):
        if self._rows is not None:
            return self._wrap(self._rows.pop(0), self._cols) if self._rows else None
        if self._cur is None or self._cur.description is None:
            return None
        r = self._cur.fetchone()
        return None if r is None else self._wrap(r, self._names())

    def fetchall(self) -> list:
        if self._rows is not None:
            rows, self._rows = self._rows, []
            return [self._wrap(r, self._cols) for r in rows]
        if self._cur is None or self._cur.description is None:
            return []
        cols = self._names()
        return [self._wrap(r, cols) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount if self._cur is not None else -1

    @property
    def description(self):
        return self._cur.description if self._cur is not None else None


class _PgConn:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        if m := _PG_PRAGMA_COLS.match(sql):  # PRAGMA table_info(t) → các cột của bảng (cid, name, type, …)
            cur = self._raw.execute(
                "SELECT ordinal_position - 1, column_name, data_type, (is_nullable = 'NO')::int, column_default, 0 "
                "FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s "
                "ORDER BY ordinal_position", (m.group(1).lower(),))
            return _PgCursor(None, cur.fetchall(), ["cid", "name", "type", "notnull", "dflt_value", "pk"])
        named = isinstance(params, dict)
        args = params if named else tuple(params or ())
        return _PgCursor(self._raw.execute(_pg_sql(sql, named), args))

    def executemany(self, sql: str, seq):
        seq = list(seq)
        if seq:
            named = isinstance(seq[0], dict)
            with self._raw.cursor() as cur:
                cur.executemany(_pg_sql(sql, named), seq if named else [tuple(p) for p in seq])

    def executescript(self, script: str):
        # bỏ chú thích "-- …" (có thể chứa dấu ;) rồi tách từng câu theo ;
        code = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
        for stmt in code.split(";"):
            if stmt.strip():
                self._raw.execute(_pg_sql(stmt, False))

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()


_tls = threading.local()  # mỗi luồng 1 kết nối Postgres dùng lại (mở kết nối mạng mỗi lần thì chậm)
PG_PING_AFTER = 60  # kết nối để không quá chừng này giây thì thử SELECT 1 trước (server có thể đã đóng kết nối rảnh)


def _pg_raw():
    raw, last = getattr(_tls, "pg", None), getattr(_tls, "pg_used", 0.0)
    if raw is not None and (raw.closed or raw.broken):
        raw = None
    if raw is not None and time.monotonic() - last > PG_PING_AFTER:
        try:
            raw.execute("SELECT 1").fetchone()
            raw.rollback()
        except Exception:  # noqa: BLE001 – kết nối chết → mở lại
            raw = None
    if raw is None:
        if not DATABASE_URL.startswith(("postgres://", "postgresql://")):
            raise RuntimeError("Thiếu DATABASE_URL (postgresql://user:mật-khẩu@host:5432/tên-db) trong .env của web")
        import psycopg
        # prepare_threshold=None: không dùng prepared statement (pooler kiểu transaction của Supabase cổng 6543 không giữ được)
        raw = psycopg.connect(DATABASE_URL, autocommit=False, connect_timeout=15, prepare_threshold=None)
    _tls.pg, _tls.pg_used = raw, time.monotonic()
    return raw


@contextmanager
def connect():
    """1 khối `with connect() as conn:` = 1 transaction (commit khi xong, lỗi thì rollback)."""
    raw = _pg_raw()
    conn = _PgConn(raw)
    try:
        yield conn
        raw.commit()
    except BaseException:
        try:
            raw.rollback()
        except Exception:  # noqa: BLE001
            _tls.pg = None  # kết nối hỏng → lần sau mở lại
        raise
    finally:
        _tls.pg_used = time.monotonic()


def table_columns(conn, table: str) -> set[str]:
    """Tên các cột của 1 bảng (information_schema)."""
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def list_tables(conn) -> list[str]:
    """Các bảng dữ liệu (bỏ bảng hệ thống)."""
    return [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
                                       "AND table_type = 'BASE TABLE' ORDER BY table_name")]


# danh mục sản phẩm ban đầu (lọc từ các đơn cũ): tên chuẩn → các cách ghi ten_sp trong đơn.
# Giá thu gợi ý ban đầu = giá shop thu của đơn mới nhất (không hủy) có giá.
SEED_PRODUCTS = {
    "Reno 16F": ["reno 16f"],
    "Cầu TC 77": ["cầu tc 77", "cau tc 77"],
    "S25 FE": ["s25fe"],
    "S26 Ultra": ["s26u"],
    "Redmi Note 15 Pro 4G 8/256": ["redmi note 15 pro 4g 8/256", "redmi note 15 pro 4g"],
    "RAM Viper 16GB": ["ram viper16gb", "ram viper 16gb", "ram viper 16g"],
    "i5 12400F": ["dt 12400f"],
    "i5 14400F": ["i5 14400f dttech"],
    "i5 14600K": ["dtshop i5 14600k", "cpu 14600k"],
    "i5 14600KF": ["dt 14600kf"],
    "A57 5G 8/128": ["a57 5g 8 /128"],
    "A17 4G": ["a17 4g thouse"],
    "A06 4/128": ["a06 4 128"],
}

DEFAULT_CARRIERS = ["SPX", "J&T", "GHN", "GHTK", "Viettel Post", "Ninja Van", "Best Express"]


# đổi mỗi khi sửa db.py (bảng, cột, dữ liệu mẫu đều nằm ở đây) → init_db chạy đầy đủ 1 lần rồi ghi vào meta
INIT_DB_KEY = "init_db_version"
INIT_DB_VERSION = hashlib.sha256(open(__file__, "rb").read()).hexdigest()[:16]


def init_db():
    """Tạo / nâng cấp bảng + dữ liệu mẫu. Cùng phiên bản db.py đã chạy rồi (meta.init_db_version) → bỏ qua, chỉ tốn
    1 câu SQL (Vercel khởi động lạnh rất thường, database ở xa: chạy đủ ~60 câu mỗi lần mất cả chục giây)."""
    try:
        with connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (INIT_DB_KEY,)).fetchone()
        if row and row[0] == INIT_DB_VERSION:
            return
    except Exception as e:  # noqa: BLE001 – database mới chưa có bảng meta → chạy đầy đủ
        log.info("Chạy init_db đầy đủ: %s", e)
    _init_db_full()
    with connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (INIT_DB_KEY, INIT_DB_VERSION))


def _init_db_full():
    with connect() as conn:  # hàm name_key() cho các câu SQL (so tên không phân biệt hoa thường, gộp khoảng trắng)
        conn.execute(PG_NAME_KEY_FUNC)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, col in PG_NOCASE_UNIQUE.items():
            conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_{col}_nocase ON {table} (lower({col}))")
        # từ lúc này mới có nhật ký đổi trạng thái; đơn đổi trước đó không có mốc giờ
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT DO NOTHING", (STATUS_LOG_SINCE_KEY, _now()))
        if not conn.execute("SELECT 1 FROM carriers LIMIT 1").fetchone():
            # lần đầu: mặc định + các đơn vị đã có trong đơn cũ
            used = [r[0] for r in conn.execute(
                "SELECT DISTINCT TRIM(don_vi_vc) FROM orders WHERE TRIM(COALESCE(don_vi_vc, '')) != ''")]
            for name in DEFAULT_CARRIERS + used:
                conn.execute("INSERT INTO carriers (ten, created_at) VALUES (?, ?) ON CONFLICT DO NOTHING", (name, _now()))
        if "mac_dinh" not in table_columns(conn, "alert_bots"):
            conn.execute("ALTER TABLE alert_bots ADD COLUMN mac_dinh INTEGER NOT NULL DEFAULT 0")
        _ensure_default_bot(conn)
        alert_cols = table_columns(conn, "alert_users")
        for col, ddl in (("username", "TEXT"), ("trang_thai", "TEXT NOT NULL DEFAULT 'da_duyet'"),
                         ("updated_at", "TEXT"), ("bot_id", "INTEGER"),
                         ("nguoi_dat_id", "INTEGER"), ("la_admin", "INTEGER NOT NULL DEFAULT 0")):
            if col not in alert_cols:
                conn.execute(f"ALTER TABLE alert_users ADD COLUMN {col} {ddl}")
        # bot alert không đọc .env nữa (token + telegram_id admin nhập ở tab 🤖 Bot alert): chép 1 lần cái đang có
        # trong .env (ALERT_BOT_TOKEN, ALERT_CHAT_IDS – trống thì ALLOWED_USER_IDS) vào bảng để alert không bị ngừng
        if not conn.execute("SELECT 1 FROM meta WHERE key = 'alert_env_imported'").fetchone():
            if (token := os.getenv("ALERT_BOT_TOKEN", "").strip()):
                conn.execute("INSERT INTO alert_bots (token, created_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
                             (token, _now()))
                _ensure_default_bot(conn)
            ids = [x for x in os.getenv("ALERT_CHAT_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()] \
                or [x for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]
            for cid in ids:
                conn.execute("INSERT INTO alert_users (chat_id, trang_thai, la_admin, created_at) VALUES (?, ?, 1, ?) "
                             "ON CONFLICT(chat_id) DO UPDATE SET la_admin = 1", (int(cid), ALERT_APPROVED, _now()))
            conn.execute("INSERT INTO meta (key, value) VALUES ('alert_env_imported', ?)", (_now(),))
        # token bot alert trước đây lưu 1 cái trong meta (đè .env cho mọi người nhận) → thành 1 dòng alert_bots,
        # người nhận chưa chọn bot dùng bot đó như trước
        if (old_token := conn.execute("SELECT value FROM meta WHERE key = 'alert_bot_token'").fetchone()):
            if (token := (old_token[0] or "").strip()):
                conn.execute("INSERT INTO alert_bots (token, created_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
                             (token, _now()))
                bot_id = conn.execute("SELECT id FROM alert_bots WHERE token = ?", (token,)).fetchone()[0]
                conn.execute("UPDATE alert_users SET bot_id = ? WHERE bot_id IS NULL", (bot_id,))
                _ensure_default_bot(conn)
            conn.execute("DELETE FROM meta WHERE key = 'alert_bot_token'")
        # người nhận alert trước đây lưu dạng JSON trong meta
        if (old_alert := conn.execute("SELECT value FROM meta WHERE key = 'alert_chats'").fetchone()):
            for cid, name in json.loads(old_alert[0] or "{}").items():
                conn.execute("INSERT INTO alert_users (chat_id, ten, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                             (int(cid), name, _now()))
            conn.execute("DELETE FROM meta WHERE key = 'alert_chats'")
        # người đặt hộ: thêm người còn thiếu từ các đơn cũ (lấy SĐT + ảnh QR mới nhất của người đó).
        # Người đã có thì giữ nguyên – không đè SĐT / QR đã sửa tay mỗi lần khởi động.
        known = {r[0] for r in conn.execute("SELECT ten_key FROM nguoi_dat")}
        for o in conn.execute("SELECT ten_nguoi_dat, sdt, qr_file_id FROM orders "
                              "WHERE TRIM(COALESCE(ten_nguoi_dat, '')) != '' ORDER BY id").fetchall():
            if name_key(o["ten_nguoi_dat"]) not in known:
                _upsert_person(conn, o["ten_nguoi_dat"], o["sdt"], o["qr_file_id"])
        conn.execute("""UPDATE orders SET qr_file_id = (SELECT p.qr_file_id FROM nguoi_dat p
                                                         WHERE p.ten_key = name_key(orders.ten_nguoi_dat))
                        WHERE qr_file_id IS NULL""")
        # danh bạ SĐT: thêm các số đã dùng trong đơn
        phone_cols = table_columns(conn, "phones")
        if "may" not in phone_cols:
            conn.execute("ALTER TABLE phones ADD COLUMN may TEXT")
        if "cua_toi" not in phone_cols:
            conn.execute("ALTER TABLE phones ADD COLUMN cua_toi INTEGER NOT NULL DEFAULT 0")
        if "da_su_dung" not in phone_cols:
            conn.execute("ALTER TABLE phones ADD COLUMN da_su_dung INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE phones SET da_su_dung = 1")  # số cũ đều lấy từ đơn → đã dùng
        known_phones = {r[0] for r in conn.execute("SELECT sdt FROM phones")}
        for (raw,) in conn.execute("SELECT DISTINCT sdt FROM orders WHERE TRIM(COALESCE(sdt, '')) != ''").fetchall():
            if (norm := norm_phone(raw)) and norm not in known_phones:
                known_phones.add(norm)
                conn.execute("INSERT INTO phones (sdt, da_su_dung, created_at) VALUES (?, 1, ?) ON CONFLICT DO NOTHING",
                             (norm, _now()))
        # migration cho DB tạo trước khi có cột trang_thai
        cols = table_columns(conn, "orders")
        if "so_luong" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN so_luong INTEGER")
        if "vc_alert_last" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN vc_alert_last TEXT")
        if "vc_near" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN vc_near INTEGER NOT NULL DEFAULT 0")
        if "vc_next_check" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN vc_next_check REAL")
        if "qr_file_id" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN qr_file_id TEXT")
        if "ten_tren_don" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN ten_tren_don TEXT")
        if "chien_dich" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN chien_dich INTEGER")
        if "bonus_thu" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN bonus_thu INTEGER DEFAULT 0")
        for col, typ in (("shop", "TEXT"), ("mau", "TEXT"), ("mo_ta", "TEXT"), ("bh_kich", "TEXT"), ("bh_thang", "INTEGER")):
            if col not in cols:  # mô tả chi tiết + bảo hành từng đơn
                conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
        if "tach_tu" not in cols:  # đơn tách ra khi bán 1 phần (sell_part): id đơn gốc
            conn.execute("ALTER TABLE orders ADD COLUMN tach_tu INTEGER")
        # bỏ "công tối thiểu" của chiến dịch (thay bằng chọn chiến dịch cho từng đơn)
        camp_cols = table_columns(conn, "campaigns")
        if "cong_min" in camp_cols:
            conn.execute("ALTER TABLE campaigns DROP COLUMN cong_min")
        if "active" not in camp_cols:
            conn.execute("ALTER TABLE campaigns ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "trang_thai" not in cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN trang_thai TEXT NOT NULL DEFAULT '{STATUS_ORDERED}'")
        for old, new in _OLD_STATUSES.items():
            conn.execute("UPDATE orders SET trang_thai = ? WHERE trang_thai = ?", (new, old))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_trang_thai ON orders(trang_thai)")
        if not conn.execute("SELECT 1 FROM meta WHERE key = 'phone_free_v1'").fetchone():
            _refresh_phones(conn, None)  # số có đơn: đang dùng / free theo trạng thái đơn
            conn.execute("INSERT INTO meta (key, value) VALUES ('phone_free_v1', '1')")
        product_cols = table_columns(conn, "products")
        for old in ("gia_thu", "gia_thu_max"):  # tên cột cũ
            if old in product_cols:
                conn.execute(f"ALTER TABLE products RENAME COLUMN {old} TO {old.replace('gia_thu', 'gia_goi_y')}")
        if not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            for name, aliases in SEED_PRODUCTS.items():
                keys = [name_key(name), *aliases]
                price = conn.execute(
                    f"""SELECT gia_thu FROM orders WHERE name_key(ten_sp) IN ({', '.join('?' * len(keys))})
                        AND gia_thu > 0 AND trang_thai != ? ORDER BY id DESC LIMIT 1""",
                    [*keys, STATUS_CANCELED]).fetchone()
                conn.execute("INSERT INTO products (ten, ten_key, gia_goi_y, created_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                             (name, name_key(name), price[0] if price else 0, _now()))
        sale_cols = table_columns(conn, "sales")
        for col in ("gia_sale_max", "cong_max", "so_luong"):
            if col not in sale_cols:
                conn.execute(f"ALTER TABLE sales ADD COLUMN {col} INTEGER")
        if "shop" not in sale_cols:
            conn.execute("ALTER TABLE sales ADD COLUMN shop TEXT")
        for col, typ in (("muc_gia", "TEXT"), ("gia_goc", "INTEGER"), ("giam_pt", "REAL"),
                         ("giam_toi_da", "INTEGER"), ("uu_tien", "INTEGER NOT NULL DEFAULT 0"),
                         ("ton_kho", "TEXT"), ("nhom", "INTEGER DEFAULT 0"), ("phan_loai", "TEXT")):
            if col not in sale_cols:
                conn.execute(f"ALTER TABLE sales ADD COLUMN {col} {typ}")
        person_cols = table_columns(conn, "nguoi_dat")
        for col, typ in (("zalo", "TEXT"), ("facebook", "TEXT"), ("ha_noi", "INTEGER")):
            if col not in person_cols:
                conn.execute(f"ALTER TABLE nguoi_dat ADD COLUMN {col} {typ}")
        if "loai" in sale_cols:  # đã bỏ phân loại sản phẩm
            conn.execute("DROP INDEX IF EXISTS idx_sales_active")
            conn.execute("ALTER TABLE sales DROP COLUMN loai")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_active ON sales(active)")
    enable_row_security()


def enable_row_security():
    """Bật Row Level Security cho mọi bảng chưa bật (Supabase: chặn đọc / ghi bảng qua Data API bằng khóa công khai,
    vd ảnh QR, tài khoản web). Web là chủ bảng nên không bị chặn (Postgres cho chủ bảng bỏ qua RLS khi không FORCE);
    không phải chủ bảng thì không ALTER được → bỏ qua."""
    try:
        with connect() as conn:
            for r in conn.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                  "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND NOT c.relrowsecurity "
                                  "ORDER BY c.relname").fetchall():
                conn.execute(f'ALTER TABLE "{r[0]}" ENABLE ROW LEVEL SECURITY')
    except Exception as e:  # noqa: BLE001
        log.warning("Không bật được Row Level Security: %s", e)


# ai đang đổi dữ liệu, ghi vào order_status_log: SOURCE theo tiến trình ("bot" / "web", đặt lúc khởi động),
# ACTOR theo từng lượt (tên Telegram trong check_access của bot, tài khoản web trong middleware auth)
SOURCE = None
STATUS_LOG_SINCE_KEY = "status_log_since"
ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar("db_actor", default=None)
# nguồn theo từng lượt, đè SOURCE: lời gọi từ bot qua API (/api/svc) chạy trong tiến trình web nhưng ghi "bot"
SOURCE_CTX: contextvars.ContextVar[str | None] = contextvars.ContextVar("db_source", default=None)


def status_log(order_id: int) -> list:
    """Các lần đổi trạng thái của 1 đơn, cũ nhất trước."""
    with connect() as conn:
        return conn.execute("SELECT * FROM order_status_log WHERE order_id = ? ORDER BY at, id", (order_id,)).fetchall()


def status_times(order_id: int | None = None) -> dict[int, dict[str, str]]:
    """order_id → {trạng thái: lúc chuyển sang trạng thái đó gần nhất} ("YYYY-MM-DD HH:MM:SS") từ order_status_log
    (mọi đơn, hoặc 1 đơn). Đổi trước khi có nhật ký (meta status_log_since) thì không có mốc."""
    sql = "SELECT order_id, den, MAX(at) AS at FROM order_status_log"
    with connect() as conn:
        rows = conn.execute(sql + (" WHERE order_id = ?" if order_id else "") + " GROUP BY order_id, den",
                            (order_id,) if order_id else ()).fetchall()
    out: dict[int, dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["order_id"], {})[r["den"]] = r["at"]
    return out


def paid_times() -> dict[int, str]:
    """order_id → lúc chuyển sang Thanh toán (công đã trả) gần nhất."""
    return {i: t[STATUS_PAID] for i, t in status_times().items() if STATUS_PAID in t}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- dùng chung cho mọi bảng ----------

def _sale_derive(conn, rec_id: int, field: str):
    """Món sale: nhập % giảm (giam_pt) + giá gốc → tự tính giá mua được; nhập thẳng giá mua được → bỏ % đã nhập."""
    if field in ("gia_sale", "gia_sale_max"):
        conn.execute("UPDATE sales SET giam_pt = NULL WHERE id = ?", (rec_id,))
    elif field in ("gia_goc", "giam_pt", "giam_toi_da"):
        r = conn.execute("SELECT gia_goc, giam_pt, giam_toi_da, muc_gia FROM sales WHERE id = ?", (rec_id,)).fetchone()
        if r and r["gia_goc"] and r["giam_pt"]:
            conn.execute("UPDATE sales SET gia_sale = ?, gia_sale_max = NULL WHERE id = ?",
                         (price_after_discount(r["gia_goc"], r["giam_pt"], r["giam_toi_da"]), rec_id))
        if r and field == "gia_goc":  # các mức giá khác có % giảm cũng tính lại theo giá gốc mới
            conn.execute("UPDATE sales SET muc_gia = ? WHERE id = ?", (sale_text.rediscount_tiers(r["muc_gia"], r["gia_goc"]), rec_id))


def insert(table: str, data: dict, user_id: int) -> int:
    if table == "sales" and data.get("gia_goc") and data.get("giam_pt"):
        data = {**data, "gia_sale_max": None,
                "gia_sale": price_after_discount(data["gia_goc"], data["giam_pt"], data.get("giam_toi_da"))}
    if table == "orders":
        # ghi rõ trạng thái: DB cũ có DEFAULT cột là tên trạng thái cũ
        data = {"trang_thai": STATUS_ORDERED, **data}
    cols = [f for f in [*(f for f, _ in TABLES[table]), *EXTRA_COLUMNS[table]] if f in data]
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}, created_by, created_at) "
            f"VALUES ({', '.join('?' * len(cols))}, ?, ?) RETURNING id",
            [*(data[c] for c in cols), user_id, _now()],
        )
        rec_id = cur.fetchone()[0]
        if table == "orders" and name_key(data.get("ten_nguoi_dat")):
            _sync_order_person(conn, rec_id)
    if table == "orders" and data.get("sdt"):
        add_phone(data["sdt"], used=True)
    return rec_id


def get(table: str, rec_id: int):
    with connect() as conn:
        return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rec_id,)).fetchone()


def insert_many(table: str, rows: list[dict], user_id: int) -> list[int]:
    return [insert(table, r, user_id) for r in rows]


def set_all_sales_active(value: int) -> int:
    with connect() as conn:
        return conn.execute("UPDATE sales SET active = ? WHERE active != ?", (value, value)).rowcount


def update(table: str, rec_id: int, field: str, value) -> bool:
    if field not in dict(TABLES[table]):
        raise ValueError(f"Trường không hợp lệ: {field}")
    # đổi tên danh mục: giữ ten_key + đổi tên ở các đơn / món sale đang dùng tên cũ
    if table == "products" and field == "ten":
        return rename_product(rec_id, value) is not None
    if table == "nguoi_dat" and field == "ten":
        return rename_person(rec_id, value) is not None
    if table == "carriers" and field == "ten":
        rename_carrier(rec_id, value)
        return True
    with connect() as conn:
        old = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rec_id,)).fetchone()
        ok = conn.execute(f"UPDATE {table} SET {field} = ? WHERE id = ?", (value, rec_id)).rowcount > 0
        if ok and table == "orders" and field == "trang_thai" and old["trang_thai"] != value:
            conn.execute("INSERT INTO order_status_log (order_id, tu, den, at, nguon, nguoi) VALUES (?, ?, ?, ?, ?, ?)",
                         (rec_id, old["trang_thai"], value, _now(), SOURCE_CTX.get() or SOURCE, ACTOR.get()))
        if ok and table == "orders" and field == "ten_nguoi_dat" and name_key(value):
            _sync_order_person(conn, rec_id)
        if ok and table == "sales":
            _sale_derive(conn, rec_id, field)
    if ok and table == "orders" and field == "sdt" and value:
        add_phone(value)
    if ok and table == "orders" and field in ("sdt", "trang_thai"):
        # đổi trạng thái / đổi số → cập nhật đang dùng / free (cả số cũ)
        with connect() as conn:
            _refresh_phones(conn, {old["sdt"], value if field == "sdt" else None})
    return ok


def set_vc_alert(order_id: int, signature: str):
    with connect() as conn:
        conn.execute("UPDATE orders SET vc_alert_last = ? WHERE id = ?", (signature, order_id))


# bot ghi giờ còn sống mỗi HEARTBEAT_SECONDS; quá BOT_STALE_SECONDS không ghi = bot coi như chết
HEARTBEAT_KEY, HEARTBEAT_SECONDS, BOT_STALE_SECONDS = "bot_heartbeat", 60, 180


def bot_heartbeat_age() -> tuple[str | None, int | None]:
    """(lần ghi cuối, số giây đã qua) của heartbeat bot; (None, None) nếu chưa ghi lần nào."""
    last = get_meta(HEARTBEAT_KEY)
    if not last:
        return None, None
    return last, int((datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds())


def get_meta(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def set_meta(key: str, value: str):
    with connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def reserve_slot(key: str, gap: float) -> float:
    """Giữ chỗ 1 lượt gọi cách lượt trước ít nhất `gap` giây, dùng chung mọi tiến trình (bot + web) qua bảng meta.

    Trả về số giây phải chờ tới lượt của mình (0 = gọi ngay); việc chờ làm ngoài DB (asyncio.sleep). Chỉ tiến trình
    web ghi database (bot gọi qua API) → khóa trong tiến trình là đủ.
    """
    with _slot_lock, connect() as conn:
        return _take_slot(conn, key, gap)


_slot_lock = threading.Lock()


def take_cooldown(key: str, seconds: float) -> float:
    """Khóa có hạn (như Redis SET NX EX) trong bảng meta, giá trị = giờ hết hạn (epoch, không lệ thuộc múi giờ).
    Trả về 0 = vừa giữ được khóa; > 0 = khóa đang bị giữ, còn chừng đó giây. Đặt + kiểm tra trong 1 câu lệnh →
    2 request cùng lúc chỉ 1 cái giữ được. Khóa cùng tiền tố đã hết hạn bị dọn luôn."""
    now = time.time()
    prefix = key.split(":", 1)[0] + ":"
    with connect() as conn:
        # CASE: Postgres không hứa xét LIKE trước → không CAST nhầm giá trị chữ của khóa meta khác
        conn.execute("DELETE FROM meta WHERE key != ? AND "
                     "(CASE WHEN key LIKE ? THEN CAST(value AS DOUBLE PRECISION) END) < ?", (key, prefix + "%", now))
        if conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value "
                        "WHERE CAST(meta.value AS DOUBLE PRECISION) <= ? RETURNING key",
                        (key, f"{now + seconds:.3f}", now)).fetchone():
            return 0
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return max(float(row[0]) - now, 1) if row else 0


def release_cooldown(key: str):
    with connect() as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def _take_slot(conn, key: str, gap: float) -> float:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    now = time.time()
    try:
        last = float(row[0]) if row else 0.0
    except ValueError:
        last = 0.0
    if last > now + 3600:  # giá trị hỏng / đồng hồ bị lùi → bỏ qua
        last = 0.0
    slot = max(now, last + gap)
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, f"{slot:.3f}"))
    return slot - now


def take_lease(key: str, seconds: float) -> str | None:
    """Giữ quyền chạy 1 việc (vd 1 lượt kiểm tra vận chuyển) trong `seconds` giây, dùng chung mọi tiến trình / máy
    (cron Vercel, bot gọi /kiemtravc, vòng lặp web Docker) qua bảng meta: 1 câu lệnh nguyên tử. Trả về mã giữ chỗ
    (đưa cho release_lease) hoặc None khi đang có nơi khác giữ và chưa hết hạn. Giá trị = '<hạn epoch 12 số>|<mã>'
    → so chuỗi = so giờ."""
    now = int(time.time())
    token = secrets.token_hex(6)
    value = f"{now + int(seconds):012d}|{token}"
    with connect() as conn:
        row = conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                           "SET value = excluded.value WHERE meta.value < ? RETURNING key",
                           (key, value, f"{now:012d}")).fetchone()
    return value if row else None


def release_lease(key: str, lease: str | None):
    if lease:
        with connect() as conn:
            conn.execute("UPDATE meta SET value = '0' WHERE key = ? AND value = ?", (key, lease))


def set_qr(order_id: int, file_id: str | None) -> bool:
    """Đặt/xóa ảnh QR của đơn → lưu luôn cho người đặt và mọi đơn của người đó (QR dùng chung)."""
    with connect() as conn:
        row = conn.execute("SELECT ten_nguoi_dat FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE orders SET qr_file_id = ? WHERE id = ?", (file_id, order_id))
        if row["ten_nguoi_dat"] and name_key(row["ten_nguoi_dat"]):
            pid = _upsert_person(conn, row["ten_nguoi_dat"])
            _set_person_qr(conn, pid, file_id)
        return True


def delete(table: str, rec_id: int) -> bool:
    with connect() as conn:
        old = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rec_id,)).fetchone()
        cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (rec_id,))
        if cur.rowcount and table == "orders":
            _refresh_phones(conn, {old["sdt"]})
            conn.execute("DELETE FROM order_status_log WHERE order_id = ?", (rec_id,))
        return cur.rowcount > 0


def all_rows(table: str):
    with connect() as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


# ---------- đơn hàng ----------

def list_orders(limit: int | None = 10, canceled: bool = False, status: str | None = None):
    """Đơn mới nhất. Mặc định bỏ đơn hủy; canceled=True chỉ lấy đơn hủy; status lọc đúng 1 trạng thái."""
    if status:
        where, param = "trang_thai = ?", status
    else:
        where, param = f"trang_thai {'=' if canceled else '!='} ?", STATUS_CANCELED
    with connect() as conn:
        # không giới hạn = bỏ LIMIT
        return conn.execute(
            f"SELECT * FROM orders WHERE {where} ORDER BY id DESC" + (" LIMIT ?" if limit is not None else ""),
            (param, limit) if limit is not None else (param,)
        ).fetchall()


def search_orders(keyword: str, limit: int | None = 20):
    """limit None / âm = không giới hạn (bỏ LIMIT: Postgres không hiểu LIMIT -1)."""
    like = f"%{keyword}%"
    unlimited = limit is None or limit < 0
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM orders
               WHERE ten_nguoi_dat LIKE ? OR sdt LIKE ? OR ma_don LIKE ?
                  OR ten_sp LIKE ? OR dia_chi LIKE ? OR ten_tren_don LIKE ? OR mau LIKE ? OR mo_ta LIKE ? OR shop LIKE ?
               ORDER BY id DESC""" + ("" if unlimited else " LIMIT ?"),
            (like,) * 9 + (() if unlimited else (limit,)),
        ).fetchall()


def customer_orders(keyword: str):
    """Tất cả đơn của khách có tên hoặc SĐT chứa keyword, gom theo tên khách."""
    like = f"%{keyword}%"
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM orders
               WHERE ten_nguoi_dat LIKE ? OR sdt LIKE ?
               ORDER BY id DESC""",
            (like, like),
        ).fetchall()


def set_vc_near(order_id: int, level: int):
    with connect() as conn:
        conn.execute("UPDATE orders SET vc_near = ? WHERE id = ?", (level, order_id))


def get_track_log(code: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM track_logs WHERE ma_don = ?", (code,)).fetchone()


def save_track_log(code: str, carrier: str, milestone: str, data: str, final: bool):
    with connect() as conn:
        conn.execute(
            """INSERT INTO track_logs (ma_don, carrier, milestone, data, final, updated_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(ma_don) DO UPDATE SET carrier = excluded.carrier, milestone = excluded.milestone,
                   data = excluded.data, final = excluded.final, updated_at = excluded.updated_at""",
            (code, carrier, milestone, data, int(final), _now()))


def unfinished_logs(older_than_hours: float):
    """Đơn không còn Đã đặt (bot không tra nữa) mà hành trình đã lưu chưa kết thúc và lâu chưa tra lại."""
    cutoff = (datetime.now() - timedelta(hours=older_than_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        return conn.execute(
            """SELECT o.* FROM orders o JOIN track_logs t ON t.ma_don = upper(replace(trim(o.ma_don), ' ', ''))
               WHERE o.trang_thai != ? AND t.final = 0 AND t.updated_at < ? ORDER BY o.id""",
            (STATUS_ORDERED, cutoff)).fetchall()


def set_vc_next(order_id: int, ts: float):
    with connect() as conn:
        conn.execute("UPDATE orders SET vc_next_check = ? WHERE id = ?", (ts, order_id))


def orders_by_status(statuses: list[str], limit: int = 200):
    """Đơn theo trạng thái, cũ nhất trước (để thấy đơn tồn lâu)."""
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM orders WHERE trang_thai IN ({', '.join('?' * len(statuses))}) "
            f"ORDER BY id LIMIT ?", (*statuses, limit)
        ).fetchall()


def month_orders_not_done(month: str):
    """Đơn chưa hoàn thành (Đã đặt, Đã nhận, Thanh toán) tạo trong tháng, cũ nhất trước."""
    with connect() as conn:
        return conn.execute("SELECT * FROM orders WHERE created_at LIKE ? AND trang_thai IN (?, ?, ?) ORDER BY id",
                            (month + "%", *UNSOLD_STATUSES)).fetchall()


def month_stats(month: str):
    """Thống kê 1 tháng ('2026-10'), không tính đơn hủy, gom theo (đã hoàn thành?, sàn).

    Mỗi dòng: hoan_thanh (1/0), nguon_don, so_don, tong_dat, tong_cong, tong_thu (giá shop thu + bonus thu).
    """
    with connect() as conn:
        return conn.execute(
            """SELECT (trang_thai = :done) AS hoan_thanh, nguon_don, COUNT(*) AS so_don,
                      COALESCE(SUM(gia_dat), 0) AS tong_dat,
                      COALESCE(SUM(cong_dat), 0) AS tong_cong,
                      COALESCE(SUM(gia_thu), 0)
                        + COALESCE(SUM(CASE WHEN gia_thu > 0 THEN bonus_thu ELSE 0 END), 0) AS tong_thu
               FROM orders WHERE created_at LIKE :prefix AND trang_thai != :canceled
               GROUP BY hoan_thanh, nguon_don ORDER BY hoan_thanh DESC, so_don DESC""",
            {"done": STATUS_DONE, "canceled": STATUS_CANCELED, "prefix": month + "%"}).fetchall()


def month_status_counts(month: str) -> dict:
    """Số đơn từng trạng thái (kể cả Đã hủy) của 1 tháng ('2026-10')."""
    with connect() as conn:
        counts = dict(conn.execute("SELECT trang_thai, COUNT(*) FROM orders WHERE created_at LIKE ? GROUP BY trang_thai",
                                   (month + "%",)).fetchall())
    return {st: counts.get(st, 0) for st in STATUSES}


def order_thu(r) -> int:
    """Tiền thu của 1 đơn = giá shop thu + bonus thu (người dùng yêu cầu 11/2026: shop thu 8tr + bonus 100k → 8tr1).
    Đơn chưa có giá shop thu (chưa bán) → 0, bonus chưa tính."""
    thu = r["gia_thu"] or 0
    return thu + ((r["bonus_thu"] or 0) if "bonus_thu" in r.keys() else 0) if thu else 0


def month_forecast(month: str = "") -> dict:
    """Thu / lãi của các đơn không hủy trong tháng ('' = mọi tháng), theo trạng thái.

    Chỉ dùng giá shop thu (+ bonus thu, order_thu) đã nhập của từng đơn (không dùng giá gợi ý sản phẩm). Đơn chưa có giá shop thu
    (0 / bỏ trống) thu = 0, không tính phần thu − giá đặt − công vào lãi, đếm ở thieu_gia.
    Thưởng chiến dịch (người dùng yêu cầu 11/2026): mỗi lần người đặt đạt mốc, tiền thưởng tính là chi phí của
    đơn mốc (db.campaign_order_seq, moc_dat) → nằm ở tháng / trạng thái của đơn đó và luôn trừ vào lãi.
    Mỗi trạng thái: so_don, gia_dat, cong, thuong, so_moc, chi (gia_dat + công + thưởng), thu,
    chi_co_gia (giá đặt + công của đơn có giá shop thu), lai (thu − chi_co_gia − thưởng), thieu_gia.
    """
    with connect() as conn:
        orders = conn.execute(
            """SELECT id, trang_thai, gia_dat, cong_dat, gia_thu, bonus_thu FROM orders
               WHERE created_at LIKE ? AND trang_thai != ?""", (month + "%", STATUS_CANCELED)).fetchall()
    seq = campaign_order_seq()
    keys = ("so_don", "gia_dat", "cong", "thuong", "so_moc", "chi", "thu", "chi_co_gia", "lai", "thieu_gia")
    out = {st: dict.fromkeys(keys, 0) for st in STATUSES if st != STATUS_CANCELED}
    for o in orders:
        r = out.setdefault(o["trang_thai"], dict.fromkeys(keys, 0))
        chi = (o["gia_dat"] or 0) + (o["cong_dat"] or 0)
        s = seq.get(o["id"])
        thuong = (s["thuong"] or 0) if s and s["moc_dat"] else 0
        r["so_don"] += 1
        r["gia_dat"] += o["gia_dat"] or 0
        r["cong"] += o["cong_dat"] or 0
        if thuong:
            r["thuong"] += thuong
            r["so_moc"] += 1
            r["lai"] -= thuong
        r["chi"] += chi + thuong
        thu = order_thu(o)
        if thu:
            r["thu"] += thu
            r["chi_co_gia"] += chi
            r["lai"] += thu - chi
        else:
            r["thieu_gia"] += 1
    return out


# ---------- người nhận bot alert ----------

ALERT_PENDING, ALERT_APPROVED, ALERT_REJECTED = "cho_duyet", "da_duyet", "tu_choi"


def list_alert_users(status: str | None = None):
    with connect() as conn:
        if status:
            return conn.execute("SELECT * FROM alert_users WHERE trang_thai = ? ORDER BY created_at",
                                (status,)).fetchall()
        return conn.execute("SELECT * FROM alert_users ORDER BY created_at").fetchall()


def get_alert_user(chat_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM alert_users WHERE chat_id = ?", (chat_id,)).fetchone()


def upsert_alert_user(chat_id: int, name: str | None, username: str | None = None,
                      status: str = ALERT_APPROVED, by: int | None = None):
    """Thêm mới hoặc cập nhật tên + trạng thái."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO alert_users (chat_id, ten, username, trang_thai, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   ten = COALESCE(excluded.ten, alert_users.ten),
                   username = COALESCE(excluded.username, alert_users.username),
                   trang_thai = excluded.trang_thai,
                   created_by = COALESCE(excluded.created_by, alert_users.created_by),
                   updated_at = excluded.updated_at""",
            (chat_id, name or None, username or None, status, by, _now(), _now()))


def remove_alert_user(chat_id: int):
    """Xóa, trả về dòng đã xóa (None nếu không có)."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM alert_users WHERE chat_id = ?", (chat_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM alert_users WHERE chat_id = ?", (chat_id,))
        return row


def set_alert_user_admin(chat_id: int, admin: bool):
    """👑 admin: nhận thêm tin đơn bị hủy / hoàn."""
    with connect() as conn:
        conn.execute("UPDATE alert_users SET la_admin = ? WHERE chat_id = ?", (int(admin), chat_id))


def set_alert_user_links(chat_id: int, bot_id: int | None, person_id: int | None):
    """Bot gửi (alert_bots.id, None = bot mặc định .env) + hồ sơ người đặt (nguoi_dat.id) của 1 người nhận."""
    with connect() as conn:
        conn.execute("UPDATE alert_users SET bot_id = ?, nguoi_dat_id = ? WHERE chat_id = ?", (bot_id, person_id, chat_id))


# ---------- các bot gửi alert (có token: không đưa vào SVC_DB_FUNCS của web) ----------

def list_alert_bots():
    with connect() as conn:
        return conn.execute("SELECT * FROM alert_bots ORDER BY id").fetchall()


def get_alert_bot(bot_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM alert_bots WHERE id = ?", (bot_id,)).fetchone()


def find_alert_bot(token: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM alert_bots WHERE token = ?", (token,)).fetchone()


def _ensure_default_bot(conn):
    """Bảng có bot thì luôn có đúng 1 bot mặc định: chưa có thì lấy bot thêm sớm nhất."""
    if not conn.execute("SELECT 1 FROM alert_bots WHERE mac_dinh = 1").fetchone():
        conn.execute("UPDATE alert_bots SET mac_dinh = 1 WHERE id = (SELECT MIN(id) FROM alert_bots)")


def default_alert_bot():
    with connect() as conn:
        return conn.execute("SELECT * FROM alert_bots WHERE mac_dinh = 1 ORDER BY id LIMIT 1").fetchone()


def set_default_alert_bot(bot_id: int):
    with connect() as conn:
        conn.execute("UPDATE alert_bots SET mac_dinh = CASE WHEN id = ? THEN 1 ELSE 0 END", (bot_id,))


def add_alert_bot(token: str, name: str | None, username: str | None) -> int:
    """Thêm bot; bot đầu tiên thành bot mặc định."""
    with connect() as conn:
        bot_id = conn.execute("INSERT INTO alert_bots (token, ten, username, created_at) VALUES (?, ?, ?, ?) RETURNING id",
                              (token, name, username, _now())).fetchone()[0]
        _ensure_default_bot(conn)
        return bot_id


def update_alert_bot(bot_id: int, **fields):
    """Sửa token / ten / username."""
    fields = {k: v for k, v in fields.items() if k in ("token", "ten", "username")}
    if fields:
        with connect() as conn:
            conn.execute(f"UPDATE alert_bots SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), bot_id))


def remove_alert_bot(bot_id: int):
    """Xóa bot; người nhận đang dùng bot này quay về bot mặc định (xóa bot mặc định → bot thêm sớm nhất còn lại
    thành mặc định). Trả về dòng đã xóa."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM alert_bots WHERE id = ?", (bot_id,)).fetchone()
        if row:
            conn.execute("UPDATE alert_users SET bot_id = NULL WHERE bot_id = ?", (bot_id,))
            conn.execute("DELETE FROM alert_bots WHERE id = ?", (bot_id,))
            _ensure_default_bot(conn)
        return row


# ---------- người đặt hộ ----------

def _upsert_person(conn, name: str, sdt: str | None = None, qr: str | None = None) -> int:
    """Thêm người đặt nếu chưa có; bổ sung SĐT / QR nếu có giá trị mới. Trả về id."""
    key = name_key(name)
    # kiểm tra trước: INSERT … ON CONFLICT DO NOTHING trùng tên vẫn tốn 1 số id → id nhảy cóc
    if not conn.execute("SELECT 1 FROM nguoi_dat WHERE ten_key = ?", (key,)).fetchone():
        conn.execute("INSERT INTO nguoi_dat (ten, ten_key, created_at) VALUES (?, ?, ?)",
                     (" ".join(name.split()), key, _now()))
    if sdt:
        conn.execute("UPDATE nguoi_dat SET sdt = ? WHERE ten_key = ?", (sdt, key))
    if qr:
        conn.execute("UPDATE nguoi_dat SET qr_file_id = ? WHERE ten_key = ?", (qr, key))
    return conn.execute("SELECT id FROM nguoi_dat WHERE ten_key = ?", (key,)).fetchone()[0]


def _set_person_qr(conn, person_id: int, file_id: str | None):
    """QR của người đặt → áp cho mọi đơn của người đó."""
    p = conn.execute("SELECT ten_key FROM nguoi_dat WHERE id = ?", (person_id,)).fetchone()
    conn.execute("UPDATE nguoi_dat SET qr_file_id = ? WHERE id = ?", (file_id, person_id))
    conn.execute("UPDATE orders SET qr_file_id = ? WHERE name_key(ten_nguoi_dat) = ?", (file_id, p["ten_key"]))


def _sync_order_person(conn, order_id: int):
    """Đơn mới / đổi tên người đặt: tạo người đặt nếu chưa có, dùng chung QR (đơn có QR mới thì cập nhật người)."""
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    pid = _upsert_person(conn, o["ten_nguoi_dat"], o["sdt"])
    if o["qr_file_id"]:
        _set_person_qr(conn, pid, o["qr_file_id"])
    else:
        qr = conn.execute("SELECT qr_file_id FROM nguoi_dat WHERE id = ?", (pid,)).fetchone()[0]
        conn.execute("UPDATE orders SET qr_file_id = ? WHERE id = ?", (qr, order_id))


def find_person(ref: str):
    """Theo id hoặc tên (không phân biệt hoa thường)."""
    with connect() as conn:
        if ref.isdigit():
            return conn.execute("SELECT * FROM nguoi_dat WHERE id = ?", (int(ref),)).fetchone()
        return conn.execute("SELECT * FROM nguoi_dat WHERE ten_key = ?", (name_key(ref),)).fetchone()


def person_open_orders(name: str | None):
    """Đơn người đặt này đang đặt, chưa trả công (Đã đặt + Đã nhận), cũ nhất trước."""
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM orders WHERE name_key(ten_nguoi_dat) = ? AND trang_thai IN ({', '.join('?' * len(UNPAID_STATUSES))}) "
            "ORDER BY id", (name_key(name), *UNPAID_STATUSES)).fetchall()


def person_qr(name: str | None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT qr_file_id FROM nguoi_dat WHERE ten_key = ?", (name_key(name),)).fetchone()
        return row[0] if row else None


def set_person_qr(person_id: int, file_id: str | None) -> bool:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM nguoi_dat WHERE id = ?", (person_id,)).fetchone():
            return False
        _set_person_qr(conn, person_id, file_id)
        return True


def add_person(data: dict) -> int | None:
    """Thêm người đặt bằng tay (web) với các trường PERSON_FIELDS; trùng tên → None."""
    name = " ".join((data.get("ten") or "").split())
    with connect() as conn:
        if conn.execute("SELECT 1 FROM nguoi_dat WHERE ten_key = ?", (name_key(name),)).fetchone():
            return None
        cols = [f for f, _ in PERSON_FIELDS if f != "ten" and data.get(f) is not None]
        cur = conn.execute(
            f"INSERT INTO nguoi_dat (ten, ten_key, {''.join(c + ', ' for c in cols)}created_at) "
            f"VALUES (?, ?, {'?, ' * len(cols)}?) RETURNING id",
            [name, name_key(name), *(data[c] for c in cols), _now()])
        return cur.fetchone()[0]


def list_people():
    """Người đặt kèm số đơn (không tính hủy), số đơn Đã đặt và công chưa trả (Đã đặt + Đã nhận); nhiều đơn nhất trước."""
    with connect() as conn:
        return conn.execute(
            """SELECT p.*, COUNT(o.id) AS so_don,
                      COALESCE(SUM(CASE WHEN o.trang_thai = :ordered THEN 1 ELSE 0 END), 0) AS so_dat,
                      COALESCE(SUM(CASE WHEN o.trang_thai IN (:ordered, :received) THEN 1 ELSE 0 END), 0) AS so_chua_tra,
                      COALESCE(SUM(CASE WHEN o.trang_thai IN (:ordered, :received) THEN o.cong_dat END), 0)
                          AS cong_chua_tra
               FROM nguoi_dat p
               LEFT JOIN orders o ON name_key(o.ten_nguoi_dat) = p.ten_key AND o.trang_thai != :canceled
               GROUP BY p.id ORDER BY so_don DESC, p.id""",
            {"ordered": STATUS_ORDERED, "received": STATUS_RECEIVED, "canceled": STATUS_CANCELED}).fetchall()


def person_search_texts() -> dict[str, str]:
    """ten_key → chữ để tìm người đặt trên web: mọi SĐT (cả dạng chuẩn hóa), tên trên đơn, địa chỉ trong các đơn của họ."""
    with connect() as conn:
        rows = conn.execute("SELECT name_key(ten_nguoi_dat) AS k, sdt, ten_tren_don, dia_chi FROM orders").fetchall()
    out: dict[str, set] = {}
    for r in rows:
        s = out.setdefault(r["k"], set())
        for v in (r["sdt"], norm_phone(r["sdt"]), r["ten_tren_don"], r["dia_chi"]):
            if v:
                s.add(str(v))
    return {k: " | ".join(sorted(v)) for k, v in out.items()}


def person_stats() -> dict[str, dict]:
    """Thống kê theo người đặt (ten_key): đơn thành công (đã nhận hàng = Đã nhận / Thanh toán / Hoàn thành), đang đặt,
    hủy, theo sàn {sàn: {ok, wait, fail}}, công đã nhận (Thanh toán / Hoàn thành) và công chưa trả (Đã đặt / Đã nhận)."""
    out: dict[str, dict] = {}
    with connect() as conn:
        rows = conn.execute("SELECT name_key(ten_nguoi_dat) AS k, trang_thai, nguon_don, cong_dat FROM orders").fetchall()
    for r in rows:
        st = out.setdefault(r["k"], {"ok": 0, "wait": 0, "fail": 0, "cong_da_nhan": 0, "cong_chua_tra": 0,
                                     "theo_san": {}})
        kind = ("fail" if r["trang_thai"] == STATUS_CANCELED else
                "wait" if r["trang_thai"] == STATUS_ORDERED else "ok")
        st[kind] += 1
        src = st["theo_san"].setdefault(r["nguon_don"] or "Khác", {"ok": 0, "wait": 0, "fail": 0})
        src[kind] += 1
        if r["trang_thai"] in UNPAID_STATUSES:
            st["cong_chua_tra"] += r["cong_dat"] or 0
        elif kind == "ok":
            st["cong_da_nhan"] += r["cong_dat"] or 0
    return out


def person_phones(name: str | None) -> list[dict]:
    """Mọi SĐT người đặt này từng dùng để đặt đơn (gộp theo số chuẩn hóa), dùng gần nhất trước:
    {sdt, so_don, ok (Đã nhận / Thanh toán / Hoàn thành), wait (Đã đặt), fail (Đã hủy), last (ngày đơn gần nhất),
    cua_toi, may, dang_dung} – cua_toi / may / dang_dung lấy từ danh bạ /sdt."""
    key = name_key(name)
    if not key:
        return []
    with connect() as conn:
        orders = conn.execute("SELECT sdt, trang_thai, created_at FROM orders WHERE name_key(ten_nguoi_dat) = ? "
                              "AND TRIM(COALESCE(sdt, '')) NOT IN ('', '0', '-') ORDER BY id DESC", (key,)).fetchall()
        book = {r["sdt"]: r for r in conn.execute("SELECT * FROM phones")}
    out: dict[str, dict] = {}
    for o in orders:
        raw = " ".join(o["sdt"].split())
        norm = norm_phone(raw)
        k = norm or raw.lower()  # số ngắn (vd chỉ 4 số cuối) không chuẩn hóa được → gộp theo chữ gõ
        if k not in out:
            ph = book.get(norm) if norm else None
            out[k] = {"sdt": norm or raw, "so_don": 0, "ok": 0, "wait": 0, "fail": 0, "last": o["created_at"][:10],
                      "cua_toi": bool(ph and ph["cua_toi"]), "may": ph["may"] if ph else None,
                      "dang_dung": bool(ph and ph["da_su_dung"])}
        d = out[k]
        d["so_don"] += 1
        d["ok" if o["trang_thai"] in (STATUS_RECEIVED, STATUS_PAID, STATUS_DONE) else
          "fail" if o["trang_thai"] == STATUS_CANCELED else "wait"] += 1
    # số ngắn (đơn chỉ ghi vài số cuối, vd "1796") trùng đuôi 1 số đầy đủ của người này → gộp vào số đầy đủ
    full = [d for d in out.values() if norm_phone(d["sdt"])]
    for k, d in list(out.items()):
        if norm_phone(d["sdt"]) or not d["sdt"].isdigit():
            continue
        match = [f for f in full if f["sdt"].endswith(d["sdt"])]
        if len(match) == 1:
            f = match[0]
            for n in ("so_don", "ok", "wait", "fail"):
                f[n] += d[n]
            f["last"] = max(f["last"], d["last"])
            del out[k]
    return sorted(out.values(), key=lambda d: d["last"], reverse=True)


def person_defaults(name: str | None) -> dict | None:
    """Thông tin lấy theo người đặt đã có (khi thêm đơn, bot + web): nguồn khách, SĐT, địa chỉ của đơn gần nhất
    (+ các SĐT / địa chỉ hay dùng để gợi ý). None nếu chưa có người này."""
    key = name_key(name)
    if not key:
        return None
    with connect() as conn:
        p = conn.execute("SELECT * FROM nguoi_dat WHERE ten_key = ?", (key,)).fetchone()
        orders = conn.execute("SELECT nguon_khach, ten_tren_don, sdt, dia_chi FROM orders WHERE name_key(ten_nguoi_dat) = ? "
                              "ORDER BY id DESC", (key,)).fetchall()
    if not p and not orders:
        return None

    def recent(field: str) -> list[str]:
        out = []
        for o in orders:
            v = " ".join((o[field] or "").split())
            if v and v not in ("0", "-") and v.lower() not in (x.lower() for x in out):
                out.append(v)
        return out[:3]

    phones = person_phones(name)  # mọi số từng dùng (gần nhất trước) + thống kê, để gợi ý
    sdts = [x["sdt"] for x in phones] or ([p["sdt"]] if p and p["sdt"] else [])
    addrs = recent("dia_chi")
    sources = recent("nguon_khach")
    names = recent("ten_tren_don")
    return {"ten": p["ten"] if p else name, "nguon_khach": sources[0] if sources else None,
            "sdt": sdts[0] if sdts else None, "sdts": sdts, "phones": phones,
            "dia_chi": addrs[0] if addrs else None, "dia_chis": addrs,
            "ten_tren_don": names[0] if names else None, "ten_tren_dons": names,
            "has_qr": bool(p and p["qr_file_id"]), "so_don": len(orders)}


def person_names(limit: int | None = None) -> list[str]:
    """Tên người đặt cho gợi ý /them: tất cả, nhiều đơn nhất trước."""
    return [p["ten"] for p in list_people()[:limit]]


def rename_person(person_id: int, new_name: str) -> int | None:
    """Đổi tên + cập nhật các đơn. None nếu trùng tên người khác; trả về số đơn được cập nhật."""
    with connect() as conn:
        p = conn.execute("SELECT * FROM nguoi_dat WHERE id = ?", (person_id,)).fetchone()
        other = conn.execute("SELECT id FROM nguoi_dat WHERE ten_key = ? AND id != ?",
                             (name_key(new_name), person_id)).fetchone()
        if not p or other:
            return None
        new_name = " ".join(new_name.split())
        conn.execute("UPDATE nguoi_dat SET ten = ?, ten_key = ? WHERE id = ?", (new_name, name_key(new_name), person_id))
        return conn.execute("UPDATE orders SET ten_nguoi_dat = ? WHERE name_key(ten_nguoi_dat) = ?",
                            (new_name, p["ten_key"])).rowcount


def merge_people(source_ids: list[int], target_id: int) -> int:
    """Gộp người đặt vào target: đơn của họ đổi tên sang target, target chưa có QR thì lấy QR của họ,
    rồi xóa họ khỏi danh sách. Trả về số đơn được chuyển."""
    with connect() as conn:
        t = conn.execute("SELECT * FROM nguoi_dat WHERE id = ?", (target_id,)).fetchone()
        if not t:
            raise ValueError("Không tìm thấy người gộp vào")
        moved = 0
        for sid in source_ids:
            p = conn.execute("SELECT * FROM nguoi_dat WHERE id = ?", (sid,)).fetchone()
            if not p or sid == target_id:
                continue
            moved += conn.execute("UPDATE orders SET ten_nguoi_dat = ? WHERE name_key(ten_nguoi_dat) = ?",
                                  (t["ten"], p["ten_key"])).rowcount
            if p["qr_file_id"] and not t["qr_file_id"]:
                _set_person_qr(conn, target_id, p["qr_file_id"])
                t = conn.execute("SELECT * FROM nguoi_dat WHERE id = ?", (target_id,)).fetchone()
            conn.execute("UPDATE alert_users SET nguoi_dat_id = ? WHERE nguoi_dat_id = ?", (target_id, sid))
            conn.execute("DELETE FROM nguoi_dat WHERE id = ?", (sid,))
        if t["qr_file_id"]:  # đơn vừa chuyển sang dùng QR của target
            _set_person_qr(conn, target_id, t["qr_file_id"])
        return moved


def delete_person(person_id: int) -> bool:
    """Xóa khỏi danh sách; các đơn vẫn giữ tên và ảnh QR."""
    with connect() as conn:
        conn.execute("UPDATE alert_users SET nguoi_dat_id = NULL WHERE nguoi_dat_id = ?", (person_id,))
        return conn.execute("DELETE FROM nguoi_dat WHERE id = ?", (person_id,)).rowcount > 0


# ---------- danh bạ SĐT ----------

def add_phone(raw: str, note: str | None = None, used: bool = False, mine: bool = False,
              device: str | None = None) -> bool:
    """Thêm vào danh bạ (bỏ qua nếu đã có, có note thì cập nhật). used=True: số vừa dùng đặt đơn;
    mine=True: số của mình. True nếu là số mới."""
    norm = norm_phone(raw)
    if not norm:
        return False
    with connect() as conn:
        new = not conn.execute("SELECT 1 FROM phones WHERE sdt = ?", (norm,)).fetchone()
        if new:
            conn.execute("INSERT INTO phones (sdt, ghi_chu, da_su_dung, created_at) VALUES (?, ?, ?, ?)",
                         (norm, note, int(used), _now()))
        if not new and note:
            conn.execute("UPDATE phones SET ghi_chu = ? WHERE sdt = ?", (note, norm))
        if not new and used:
            conn.execute("UPDATE phones SET da_su_dung = 1 WHERE sdt = ?", (norm,))
        if mine:
            conn.execute("UPDATE phones SET cua_toi = 1 WHERE sdt = ?", (norm,))
        if device:
            conn.execute("UPDATE phones SET may = ? WHERE sdt = ?", (device, norm))
        return new


# đơn ở các trạng thái này không còn giữ số → số free
PHONE_FREE_STATUSES = (STATUS_DONE, STATUS_CANCELED)


def _refresh_phones(conn, raws: set | None):
    """Số có đơn chưa Hoàn thành/Hủy → đang dùng (1), mọi đơn đã xong → free (0).
    raws=None: mọi số có đơn. Số chưa từng có đơn (thêm bằng /themsdt) giữ nguyên."""
    wanted = None if raws is None else {norm_phone(r) for r in raws if r} - {""}
    busy: dict[str, bool] = {}
    for row in conn.execute("SELECT sdt, trang_thai FROM orders WHERE TRIM(COALESCE(sdt, '')) != ''").fetchall():
        norm = norm_phone(row["sdt"])
        if norm and (wanted is None or norm in wanted):
            busy[norm] = busy.get(norm, False) or row["trang_thai"] not in PHONE_FREE_STATUSES
    for norm, b in busy.items():
        conn.execute("UPDATE phones SET da_su_dung = ? WHERE sdt = ?", (int(b), norm))


def set_phone_mine(phone_id: int, mine: bool) -> bool:
    with connect() as conn:
        return conn.execute("UPDATE phones SET cua_toi = ? WHERE id = ?", (int(mine), phone_id)).rowcount > 0


def set_phone_used(phone_id: int, used: bool) -> bool:
    with connect() as conn:
        return conn.execute("UPDATE phones SET da_su_dung = ? WHERE id = ?", (int(used), phone_id)).rowcount > 0


def get_phone(ref: str):
    """Tìm theo id (số ngắn) hoặc SĐT."""
    with connect() as conn:
        if ref.isdigit() and len(ref) < 6:
            return conn.execute("SELECT * FROM phones WHERE id = ?", (int(ref),)).fetchone()
        return conn.execute("SELECT * FROM phones WHERE sdt = ?", (norm_phone(ref),)).fetchone()


def my_phones() -> dict[str, str | None]:
    """Số của tôi trong danh bạ: {SĐT chuẩn hóa: máy đang lắp SIM} (bot gắn 📱 sau SĐT của đơn, lấy 1 lần mỗi lượt)."""
    with connect() as conn:
        return {r["sdt"]: r["may"] for r in conn.execute("SELECT sdt, may FROM phones WHERE cua_toi = 1")}


def phone_by_number(raw: str | None):
    """Dòng danh bạ của 1 SĐT (so theo số chuẩn hóa, không hiểu số ngắn là id như get_phone)."""
    norm = norm_phone(raw)
    if not norm:
        return None
    with connect() as conn:
        return conn.execute("SELECT * FROM phones WHERE sdt = ?", (norm,)).fetchone()


def delete_phone(phone_id: int) -> bool:
    """Xóa khỏi danh bạ; các đơn vẫn giữ SĐT."""
    with connect() as conn:
        return conn.execute("DELETE FROM phones WHERE id = ?", (phone_id,)).rowcount > 0


def _orders_with_phone():
    with connect() as conn:
        return [(norm_phone(r["sdt"]), r) for r in conn.execute(
            "SELECT * FROM orders WHERE TRIM(COALESCE(sdt, '')) != '' ORDER BY id DESC").fetchall()]


def list_phones():
    """Danh bạ kèm số đơn (không tính đơn hủy) và số địa chỉ khác nhau, nhiều đơn nhất trước."""
    with connect() as conn:
        phones = conn.execute("SELECT * FROM phones").fetchall()
    stats: dict[str, list] = {}
    all_orders: dict[str, list] = {}  # gồm cả đơn hủy, để đếm giao thất bại
    for norm, r in _orders_with_phone():
        all_orders.setdefault(norm, []).append(r)
        if r["trang_thai"] != STATUS_CANCELED:
            stats.setdefault(norm, []).append(r)
    rows = []
    for p in phones:
        orders = stats.get(p["sdt"], [])
        addrs = {" ".join((o["dia_chi"] or "").lower().split()) for o in orders if o["dia_chi"]}
        # theo sàn: thành công (đã nhận hàng = Đã nhận / Thanh toán / Hoàn thành) · thất bại (Đã hủy) · đang giao (Đã đặt)
        by_src: dict[str, dict[str, int]] = {}
        for o in all_orders.get(p["sdt"], []):
            kind = ("fail" if o["trang_thai"] == STATUS_CANCELED else
                    "wait" if o["trang_thai"] == STATUS_ORDERED else "ok")
            src = by_src.setdefault(o["nguon_don"] or "?", {"ok": 0, "fail": 0, "wait": 0})
            src[kind] += 1
        rows.append({**dict(p), "so_don": len(orders), "so_dia_chi": len(addrs), "theo_san": by_src,
                     "don_dang_dung": [o["id"] for o in orders if o["trang_thai"] not in PHONE_FREE_STATUSES],
                     "don_da_xong": [o["id"] for o in orders if o["trang_thai"] in PHONE_FREE_STATUSES],
                     "lan_cuoi": orders[0]["created_at"] if orders else None})
    rows.sort(key=lambda x: (-x["so_don"], x["lan_cuoi"] or "", x["id"]))
    return rows


def orders_by_phone(raw: str):
    """Đơn có SĐT trùng (hoặc chứa đoạn số) với raw, mới nhất trước."""
    digits = norm_phone(raw) or re.sub(r"\D", "", raw)
    if not digits:
        return []
    return [r for norm, r in _orders_with_phone() if norm == digits or (len(digits) < 10 and digits in norm)]


# ---------- đơn vị vận chuyển ----------

def list_carriers():
    """Đơn vị vận chuyển kèm số đơn đang dùng."""
    with connect() as conn:
        return conn.execute(
            """SELECT c.id, c.ten, COUNT(o.id) AS so_don FROM carriers c
               LEFT JOIN orders o ON lower(o.don_vi_vc) = lower(c.ten)
               GROUP BY c.id ORDER BY c.id""").fetchall()


def carrier_names() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT ten FROM carriers ORDER BY id")]


def add_carrier(name: str) -> bool:
    with connect() as conn:
        return conn.execute("INSERT INTO carriers (ten, created_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
                            (name, _now())).rowcount > 0


def get_carrier(carrier_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM carriers WHERE id = ?", (carrier_id,)).fetchone()


def rename_carrier(carrier_id: int, new_name: str) -> int:
    """Đổi tên và cập nhật luôn các đơn đang dùng tên cũ. Trả về số đơn được cập nhật."""
    with connect() as conn:
        old = conn.execute("SELECT ten FROM carriers WHERE id = ?", (carrier_id,)).fetchone()[0]
        conn.execute("UPDATE carriers SET ten = ? WHERE id = ?", (new_name, carrier_id))
        return conn.execute("UPDATE orders SET don_vi_vc = ? WHERE lower(don_vi_vc) = lower(?)",
                            (new_name, old)).rowcount


def delete_carrier(carrier_id: int) -> bool:
    """Xóa khỏi danh sách chọn; các đơn cũ vẫn giữ tên."""
    with connect() as conn:
        return conn.execute("DELETE FROM carriers WHERE id = ?", (carrier_id,)).rowcount > 0


# ---------- sổ địa chỉ đặt hàng ----------

def list_addresses(keyword: str | None = None):
    """Địa chỉ đã lưu, mới nhất trước; keyword: lọc theo nội dung (không phân biệt hoa/thường)."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM addresses ORDER BY id DESC").fetchall()
    kw = " ".join((keyword or "").split()).casefold()  # lọc bằng Python (casefold: chữ có dấu)
    return [r for r in rows if kw in " ".join(r["noi_dung"].split()).casefold()] if kw else rows


def add_address(text: str, user_id: int | None = None) -> int:
    text = (text or "").strip()
    if not text:
        raise ValueError("Địa chỉ trống")
    return insert("addresses", {"noi_dung": text}, user_id)


# ---------- sổ shop (xuất VAT) ----------

def list_shops() -> dict:
    """{book: shop trong sổ + số đơn / số dòng sale cùng tên, seen: tên shop gặp ở đơn / sale nhưng chưa có trong sổ}."""
    with connect() as conn:
        book = [dict(r) for r in conn.execute("SELECT * FROM shops ORDER BY ten")]
        used = {}
        for table in ("orders", "sales"):
            for r in conn.execute(f"SELECT name_key(shop) AS k, MIN(TRIM(shop)) AS ten, COUNT(*) AS n FROM {table} "
                                  "WHERE TRIM(COALESCE(shop, '')) != '' GROUP BY name_key(shop)"):
                u = used.setdefault(r["k"], {"ten": r["ten"], "orders": 0, "sales": 0})
                u[table] = r["n"]
    for b in book:
        u = used.pop(b["ten_key"], {})
        b["don"], b["sale"] = u.get("orders", 0), u.get("sales", 0)
    seen = sorted(({"ten": u["ten"], "don": u["orders"], "sale": u["sales"]} for u in used.values()),
                  key=lambda u: -(u["don"] + u["sale"]))
    return {"book": book, "seen": seen}


def save_shop(sid: int | None, ten: str, vat, ghi_chu: str = "") -> int:
    """Thêm (sid None) / sửa 1 shop trong sổ. vat 1 / 0 / None. Trùng tên shop khác → ValueError.
    Đổi tên shop trong sổ thì đổi luôn tên đó trên đơn / sale (giữ khớp)."""
    ten = " ".join((ten or "").split())
    if not ten:
        raise ValueError("Thiếu tên shop")
    key = name_key(ten)
    vat = None if vat in (None, "") else (1 if str(vat) in ("1", "true", "True") else 0)
    ghi_chu = (ghi_chu or "").strip() or None
    with connect() as conn:
        other = conn.execute("SELECT id FROM shops WHERE ten_key = ?", (key,)).fetchone()
        if other and other["id"] != sid:
            raise ValueError(f"Đã có shop “{ten}” trong sổ")
        if sid is None:
            return conn.execute("INSERT INTO shops (ten, ten_key, vat, ghi_chu, updated_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
                                (ten, key, vat, ghi_chu, _now())).fetchone()[0]
        old = conn.execute("SELECT ten_key FROM shops WHERE id = ?", (sid,)).fetchone()
        if not old:
            raise LookupError("Không tìm thấy shop")
        conn.execute("UPDATE shops SET ten = ?, ten_key = ?, vat = ?, ghi_chu = ?, updated_at = ? WHERE id = ?",
                     (ten, key, vat, ghi_chu, _now(), sid))
        if old["ten_key"] != key:
            for table in ("orders", "sales"):
                conn.execute(f"UPDATE {table} SET shop = ? WHERE name_key(shop) = ?", (ten, old["ten_key"]))
        return sid


def delete_shop(sid: int) -> bool:
    """Chỉ xóa khỏi sổ; tên shop trên đơn / sale giữ nguyên."""
    with connect() as conn:
        return conn.execute("DELETE FROM shops WHERE id = ?", (sid,)).rowcount > 0


def shop_choices() -> list[str]:
    """Tên shop để gợi ý khi gõ (sổ shop trước, rồi shop đã gặp ở đơn / sale)."""
    d = list_shops()
    return [b["ten"] for b in d["book"]] + [u["ten"] for u in d["seen"]]


# ---------- sổ địa chỉ kho / bưu cục của hãng ----------

def list_warehouses():
    with connect() as conn:
        return conn.execute("SELECT * FROM warehouses ORDER BY ten").fetchall()


def save_warehouse(wid: int | None, ten: str, dia_chi: str = "", ghi_chu: str = "") -> int:
    """Thêm (wid None) hoặc sửa 1 kho. Tên trùng kho khác (không phân biệt hoa thường) → ValueError."""
    ten = " ".join((ten or "").split())
    if not ten:
        raise ValueError("Thiếu tên kho / bưu cục (ghi đúng như trong hành trình)")
    key = name_key(ten)
    dia_chi, ghi_chu = (dia_chi or "").strip() or None, (ghi_chu or "").strip() or None
    with connect() as conn:
        other = conn.execute("SELECT id FROM warehouses WHERE ten_key = ?", (key,)).fetchone()
        if other and other["id"] != wid:
            raise ValueError(f"Đã có kho “{ten}” (#{other['id']})")
        if wid is None:
            return conn.execute("INSERT INTO warehouses (ten, ten_key, dia_chi, ghi_chu, updated_at) VALUES (?, ?, ?, ?, ?) "
                                "RETURNING id", (ten, key, dia_chi, ghi_chu, _now())).fetchone()[0]
        if not conn.execute("UPDATE warehouses SET ten = ?, ten_key = ?, dia_chi = ?, ghi_chu = ?, updated_at = ? WHERE id = ?",
                            (ten, key, dia_chi, ghi_chu, _now(), wid)).rowcount:
            raise LookupError("Không tìm thấy kho")
        return wid


def delete_warehouse(wid: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM warehouses WHERE id = ?", (wid,)).rowcount > 0


# ---------- sản phẩm ----------

def list_products(keyword: str | None = None):
    """Sản phẩm kèm số đơn cùng tên (không tính hủy), theo tên."""
    sql = """SELECT p.*, COUNT(o.id) AS so_don FROM products p
             LEFT JOIN orders o ON name_key(o.ten_sp) = p.ten_key AND o.trang_thai != ?"""
    params = [STATUS_CANCELED]
    if keyword:
        sql += " WHERE p.ten_key LIKE ?"
        params.append(f"%{name_key(keyword)}%")
    with connect() as conn:
        return conn.execute(sql + " GROUP BY p.id ORDER BY p.ten_key", params).fetchall()


def product_names() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT ten FROM products ORDER BY ten_key")]


def product_picks() -> list[tuple[str, bool]]:
    """Nút chọn tên sản phẩm cho /them, /themsale: (tên, đang sale). Món đang sale theo thứ tự ưu tiên,
    rồi tới danh mục sản phẩm, đơn nhiều trước; không trùng tên (name_key)."""
    with connect() as conn:
        products = conn.execute(
            """SELECT p.ten FROM products p
               LEFT JOIN (SELECT name_key(ten_sp) k, COUNT(*) n FROM orders GROUP BY k) o ON o.k = p.ten_key
               ORDER BY COALESCE(o.n, 0) DESC, p.ten_key""").fetchall()
    seen, out = set(), []
    for name, on_sale in [(r["ten_sp"], True) for r in list_sales()] + [(r[0], False) for r in products]:
        if name and name_key(name) not in seen:
            seen.add(name_key(name))
            out.append((name, on_sale))
    return out


def find_product(ref: str):
    """Theo id hoặc tên (không phân biệt hoa thường)."""
    with connect() as conn:
        if ref.isdigit():
            return conn.execute("SELECT * FROM products WHERE id = ?", (int(ref),)).fetchone()
        return conn.execute("SELECT * FROM products WHERE ten_key = ?", (name_key(ref),)).fetchone()


def product_prices() -> dict[str, tuple[int, int | None]]:
    """{ten_key: (thấp, cao)} của sản phẩm có giá thu gợi ý (web tự điền giá shop thu khi thêm đơn)."""
    with connect() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT ten_key, gia_goi_y, gia_goi_y_max FROM products WHERE gia_goi_y > 0")}


def product_price(name: str | None) -> tuple[int, int | None]:
    """Giá shop thu gợi ý (thấp, cao) của sản phẩm cùng tên; (0, None) nếu chưa có."""
    with connect() as conn:
        row = conn.execute("SELECT gia_goi_y, gia_goi_y_max FROM products WHERE ten_key = ?",
                           (name_key(name),)).fetchone()
        return (row[0], row[1]) if row else (0, None)


def add_product(name: str, gia: int = 0, gia_max: int | None = None) -> bool:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO products (ten, ten_key, gia_goi_y, gia_goi_y_max, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (" ".join(name.split()), name_key(name), gia, gia_max, _now())).rowcount > 0


def set_product_price(product_id: int, gia: int, gia_max: int | None = None) -> bool:
    with connect() as conn:
        return conn.execute("UPDATE products SET gia_goi_y = ?, gia_goi_y_max = ?, updated_at = ? WHERE id = ?",
                            (gia, gia_max, _now(), product_id)).rowcount > 0


def name_taken(table: str, rec_id: int, name: str) -> bool:
    """Bảng danh mục (products / nguoi_dat / carriers) đã có dòng khác cùng tên."""
    sql, key = ((f"SELECT 1 FROM {table} WHERE ten_key = ? AND id != ?", name_key(name))
                if table in ("products", "nguoi_dat") else
                ("SELECT 1 FROM carriers WHERE lower(ten) = lower(?) AND id != ?", " ".join(name.split())))
    with connect() as conn:
        return conn.execute(sql, (key, rec_id)).fetchone() is not None


def rename_product(product_id: int, new_name: str) -> int | None:
    """Đổi tên, cập nhật cả đơn / món sale cùng tên cũ. Trả về số dòng được cập nhật, None nếu trùng tên khác."""
    new_name, key = " ".join(new_name.split()), name_key(new_name)
    with connect() as conn:
        old = conn.execute("SELECT ten_key FROM products WHERE id = ?", (product_id,)).fetchone()[0]
        if key != old and conn.execute("SELECT 1 FROM products WHERE ten_key = ?", (key,)).fetchone():
            return None
        conn.execute("UPDATE products SET ten = ?, ten_key = ?, updated_at = ? WHERE id = ?",
                     (new_name, key, _now(), product_id))
        conn.execute("UPDATE product_images SET ten_key = ? WHERE ten_key = ?", (key, old))
        return sum(conn.execute(f"UPDATE {t} SET ten_sp = ? WHERE name_key(ten_sp) = ?", (new_name, old)).rowcount
                   for t in ("orders", "sales"))


def delete_product(product_id: int) -> bool:
    """Xóa khỏi danh mục; đơn / món sale cũ vẫn giữ tên."""
    with connect() as conn:
        return conn.execute("DELETE FROM products WHERE id = ?", (product_id,)).rowcount > 0


# ---------- ảnh sản phẩm ----------

MAX_PRODUCT_IMAGES = 10  # = số ảnh tối đa trong 1 album Telegram (/sale gửi 1 album / sản phẩm)


def add_product_image(name: str, file_id: str) -> int | None:
    """Thêm ảnh cho sản phẩm (theo tên). Đủ MAX_PRODUCT_IMAGES ảnh thì không thêm, trả về None."""
    key = name_key(name)
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM product_images WHERE ten_key = ?", (key,)).fetchone()[0] >= MAX_PRODUCT_IMAGES:
            return None
        return conn.execute("INSERT INTO product_images (ten_key, file_id, created_at) VALUES (?, ?, ?) RETURNING id",
                            (key, file_id, _now())).fetchone()[0]


def product_images(name: str) -> list:
    """Ảnh của sản phẩm (theo tên), cũ trước."""
    with connect() as conn:
        return conn.execute("SELECT * FROM product_images WHERE ten_key = ? ORDER BY id", (name_key(name),)).fetchall()


def product_images_map() -> dict[str, list]:
    """{ten_key: [ảnh…]} của mọi sản phẩm có ảnh."""
    out: dict[str, list] = {}
    with connect() as conn:
        for r in conn.execute("SELECT * FROM product_images ORDER BY id"):
            out.setdefault(r["ten_key"], []).append(r)
    return out


def get_product_image(image_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM product_images WHERE id = ?", (image_id,)).fetchone()


def delete_product_image(image_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,)).rowcount > 0


def product_name_known(name: str) -> str | None:
    """Tên hiển thị của sản phẩm trong danh mục hoặc món sale cùng tên (để gắn ảnh); không có thì None."""
    key = name_key(name)
    with connect() as conn:
        row = (conn.execute("SELECT ten FROM products WHERE ten_key = ?", (key,)).fetchone()
               or conn.execute("SELECT ten_sp FROM sales WHERE name_key(ten_sp) = ? ORDER BY id DESC LIMIT 1",
                               (key,)).fetchone())
    return row[0] if row else None


# ---------- săn sale ----------

def shop_names(limit: int = 9) -> list[str]:
    """Các shop hay dùng gần đây (gợi ý khi /themsale)."""
    with connect() as conn:
        return [r[0] for r in conn.execute(
            """SELECT MIN(TRIM(shop)) FROM sales WHERE TRIM(COALESCE(shop, '')) != ''
               GROUP BY name_key(shop) ORDER BY MAX(id) DESC LIMIT ?""", (limit,))]


def list_sales(active_only: bool = True, keyword: str | None = None, nhom: int | None = None):
    """nhom: None = mọi nhóm, 0 = món chưa chia nhóm, N = nhóm N."""
    sql = "SELECT * FROM sales WHERE 1=1"
    params = []
    if nhom is not None:
        sql += " AND COALESCE(nhom, 0) = ?"
        params.append(nhom)
    if active_only:
        sql += " AND active = 1"
    if keyword:
        sql += " AND (ten_sp LIKE ? OR shop LIKE ?)"
        params += [f"%{keyword}%"] * 2
    # sản phẩm theo thứ tự ưu tiên nhỏ nhất của các shop (1 lên đầu; chưa xếp = sau cùng, theo món thêm trước),
    # trong sản phẩm: shop có thứ tự trước
    rank = "CASE WHEN uu_tien > 0 THEN uu_tien ELSE 1000000000 END"
    sql = f"""SELECT * FROM ({sql}) ORDER BY MIN({rank}) OVER (PARTITION BY name_key(ten_sp)),
                 MIN(id) OVER (PARTITION BY name_key(ten_sp)), {rank}, id"""
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


CAMPAIGN_STATUSES = (STATUS_PAID, STATUS_DONE)  # đơn được tính thưởng: đã trả công (đã / chưa bán)


def list_campaigns():
    """Chiến dịch mới nhất (ngày bắt đầu) trước."""
    with connect() as conn:
        return conn.execute("SELECT * FROM campaigns ORDER BY tu DESC, id DESC").fetchall()


def get_campaign(cid: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()


def save_campaign(cid: int | None, data: dict) -> int:
    """Thêm (cid None) hoặc sửa chiến dịch: data = ten, tu, den, moc_don, thuong, active."""
    cols = ["ten", "tu", "den", "moc_don", "thuong", "active"]
    with connect() as conn:
        if cid is None:
            return conn.execute(f"INSERT INTO campaigns ({', '.join(cols)}, created_at) VALUES ({'?, ' * len(cols)}?) "
                                "RETURNING id", [*(data[c] for c in cols), _now()]).fetchone()[0]
        conn.execute(f"UPDATE campaigns SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                     [*(data[c] for c in cols), cid])
        return cid


def delete_campaign(cid: int) -> bool:
    """Xóa chiến dịch; các đơn đã gán thành "không thuộc chiến dịch"."""
    with connect() as conn:
        conn.execute("UPDATE orders SET chien_dich = NULL WHERE chien_dich = ?", (cid,))
        return conn.execute("DELETE FROM campaigns WHERE id = ?", (cid,)).rowcount > 0


def set_campaign_active(cid: int, active: int) -> bool:
    with connect() as conn:
        return conn.execute("UPDATE campaigns SET active = ? WHERE id = ?", (1 if active else 0, cid)).rowcount > 0


def campaign_choices(current: int | None = None) -> list[tuple[int, str]]:
    """Chiến dịch để chọn cho 1 đơn: đang bật (active), bắt đầu sớm trước; kèm chiến dịch đơn đang gán."""
    cs = [c for c in list_campaigns() if c["active"] or c["id"] == current]
    cs.sort(key=lambda c: c["tu"])
    return [(c["id"], c["ten"]) for c in cs]


def campaign_name(cid: int | None) -> str | None:
    c = get_campaign(cid) if cid else None
    return c["ten"] if c else None


SPLIT_FINAL_STATUSES = (STATUS_DONE, STATUS_CANCELED)  # đơn đã bán / đã hủy: không tách nữa


def split_order(order_id: int, qty: int) -> int:
    """Tách qty cái của đơn nhiều cái thành ĐƠN MỚI (người dùng yêu cầu 11/2026, vd #129 2 cái → 2 đơn 1 cái): đơn mới
    (tach_tu = id đơn này) chép mọi cột (ngày tạo, người đặt, mã vận đơn, QR, chiến dịch, trạng thái …) + nhật ký trạng
    thái; giá đặt / công / giá shop thu chia theo số cái (đơn này giữ phần còn lại → tổng không đổi). Đơn tách không
    tính thêm lần cho mốc thưởng chiến dịch. Trả về id đơn mới."""
    r = get("orders", order_id)
    if not r:
        raise ValueError("Không tìm thấy đơn")
    n = int(r["so_luong"] or 1)
    if not 1 <= qty < n:
        raise ValueError(f"Đơn #{order_id} có {n} cái – số cái tách ra phải từ 1 tới {n - 1}")
    if r["trang_thai"] in SPLIT_FINAL_STATUSES:
        raise ValueError(f"Đơn #{order_id} đang {r['trang_thai']} – không tách được")
    share = lambda total: None if total is None else round(total * qty / n)  # noqa: E731
    new = {"so_luong": qty, "tach_tu": order_id,
           **{f: share(r[f]) for f in ("gia_dat", "cong_dat", "gia_thu", "bonus_thu")}}
    keep = {"so_luong": n - qty, **{f: None if r[f] is None else r[f] - new[f] for f in ("gia_dat", "cong_dat", "gia_thu", "bonus_thu")}}
    with connect() as conn:  # 1 transaction: đơn mới + nhật ký + bớt đơn gốc
        cols = [c for c in table_columns(conn, "orders") if c != "id"]
        new_id = conn.execute(
            f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) RETURNING id",
            [new.get(c, r[c]) for c in cols]).fetchone()[0]
        conn.execute("INSERT INTO order_status_log (order_id, tu, den, at, nguon, nguoi) "
                     "SELECT CAST(? AS INTEGER), tu, den, at, nguon, nguoi FROM order_status_log WHERE order_id = ? ORDER BY id",
                     (new_id, order_id))
        conn.execute(f"UPDATE orders SET {', '.join(f'{k} = ?' for k in keep)} WHERE id = ?", [*keep.values(), order_id])
    return new_id


def split_all(order_id: int) -> list[int]:
    """Tách đơn n cái thành n đơn 1 cái (đơn này giữ 1 cái). Trả về id các đơn mới."""
    r = get("orders", order_id)
    if not r:
        raise ValueError("Không tìm thấy đơn")
    n = int(r["so_luong"] or 1)
    if n < 2:
        raise ValueError(f"Đơn #{order_id} chỉ có {n} cái – không cần tách")
    return [split_order(order_id, 1) for _ in range(n - 1)]


def sell_part(order_id: int, qty: int, unit_price: int | None) -> int:
    """Bán qty cái của đơn nhiều cái: số cái còn lại tách thành đơn mới (split_order, giữ trạng thái cũ), đơn này giữ
    qty cái → Hoàn thành với giá shop thu unit_price × qty (bỏ trống = giá 1 cái đang ghi). Trả về id đơn tách."""
    r = get("orders", order_id)
    if not r:
        raise ValueError("Không tìm thấy đơn")
    n = int(r["so_luong"] or 1)
    if not 1 <= qty < n:
        raise ValueError(f"Đơn #{order_id} có {n} cái – bán 1 phần thì số cái phải từ 1 tới {n - 1}")
    price = unit_price or (round(r["gia_thu"] / n) if r["gia_thu"] else 0)
    if not price:
        raise ValueError("Nhập giá shop thu 1 cái")
    new_id = split_order(order_id, n - qty)
    update("orders", order_id, "gia_thu", price * qty)
    update("orders", order_id, "trang_thai", STATUS_DONE)  # ghi nhật ký đổi trạng thái như bán thường
    return new_id


def campaign_stats(c) -> dict:
    """Thưởng từng người đặt của 1 chiến dịch: đơn gán vào chiến dịch (orders.chien_dich).
    dat = đơn 💵 Thanh toán / ✅ Hoàn thành (được tính), lan = dat // moc_don, thuong = lan × thưởng,
    con = còn bao nhiêu đơn nữa tới lần thưởng tiếp, cho = đơn đã gán nhưng chưa trả công (chưa tính) = dang_dat (⏳ Đã đặt) + nhan (📦 Đã nhận).
    cong = Σ công đặt mọi đơn đã gán (không tính hủy) = cong_da_tra (đơn đạt) + cong_chua_tra (đơn chờ)."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE chien_dich = ? AND trang_thai != ?",
                            (c["id"], STATUS_CANCELED)).fetchall()
    people: dict[str, dict] = {}
    for r in rows:
        p = people.setdefault(name_key(r["ten_nguoi_dat"]), {"ten": r["ten_nguoi_dat"], "dat": 0, "cho": 0, "dang_dat": 0,
                                                              "nhan": 0, "ids": [], "cong_da_tra": 0, "cong_chua_tra": 0})
        p["cong_da_tra" if r["trang_thai"] in CAMPAIGN_STATUSES else "cong_chua_tra"] += r["cong_dat"] or 0
        if r["tach_tu"]:  # phần còn lại tách ra khi bán 1 phần: vẫn là đơn gốc → không tính thêm đơn
            continue
        if r["trang_thai"] in CAMPAIGN_STATUSES:
            p["dat"] += 1
            p["ids"].append(r["id"])
        else:
            p["cho"] += 1
            p["nhan" if r["trang_thai"] == STATUS_RECEIVED else "dang_dat"] += 1
    moc = max(c["moc_don"] or 1, 1)
    for p in people.values():
        p["lan"] = p["dat"] // moc
        p["thuong"] = p["lan"] * (c["thuong"] or 0)
        p["con"] = moc - p["dat"] % moc
    for p in people.values():
        p["tong"] = p["dat"] + p["cho"]  # mọi đơn đã gán (không tính hủy)
        p["cong"] = p["cong_da_tra"] + p["cong_chua_tra"]
    # nhiều đơn nhất trước (người dùng yêu cầu), bằng nhau thì nhiều đơn đạt hơn trước
    ps = sorted(people.values(), key=lambda p: (-p["tong"], -p["dat"], p["ten"].lower()))
    return {"people": ps, "tong_thuong": sum(p["thuong"] for p in ps), "tong_dat": sum(p["dat"] for p in ps),
            "tong_cho": sum(p["cho"] for p in ps), "tong_dang_dat": sum(p["dang_dat"] for p in ps),
            "tong_nhan": sum(p["nhan"] for p in ps), "tong_cong": sum(p["cong"] for p in ps),
            "tong_cong_chua_tra": sum(p["cong_chua_tra"] for p in ps)}


def campaign_order_seq() -> dict[int, dict]:
    """Đơn được tính thưởng (💵 Thanh toán / ✅ Hoàn thành, gán chiến dịch) → thứ tự của nó với người đặt đó trong chiến dịch:
    {order_id: {n, moc, thuong, moc_dat}} — n = đơn thứ mấy (theo lúc trả công; đơn trả công trước khi có nhật ký
    xếp trước, theo id), moc_dat = n chia hết mốc → đơn này làm người đặt đạt 1 lần thưởng (người dùng yêu cầu 11/2026)."""
    paid = {i: t[STATUS_PAID] for i, t in status_times().items() if STATUS_PAID in t}
    out: dict[int, dict] = {}
    with connect() as conn:
        camps = conn.execute("SELECT id, moc_don, thuong FROM campaigns").fetchall()
        rows = conn.execute(f"SELECT id, chien_dich, name_key(ten_nguoi_dat) AS k FROM orders WHERE chien_dich IS NOT NULL "
                            f"AND tach_tu IS NULL AND trang_thai IN ({', '.join('?' * len(CAMPAIGN_STATUSES))})",
                            CAMPAIGN_STATUSES).fetchall()
    camp = {c["id"]: c for c in camps}
    groups: dict[tuple, list] = {}
    for r in rows:
        if r["chien_dich"] in camp:
            groups.setdefault((r["chien_dich"], r["k"]), []).append(r["id"])
    for (cid, _), ids in groups.items():
        moc = max(camp[cid]["moc_don"] or 1, 1)
        ids.sort(key=lambda i: (i in paid, paid.get(i, ""), i))
        for n, i in enumerate(ids, 1):
            out[i] = {"n": n, "moc": moc, "thuong": camp[cid]["thuong"] or 0, "moc_dat": n % moc == 0}
    return out


def sale_groups(active_only: bool = True) -> list[tuple[int, int]]:
    """[(nhóm, số món)] của các nhóm đã dùng (không gồm 0 = chưa chia nhóm), nhóm nhỏ trước."""
    with connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT nhom, COUNT(*) FROM sales WHERE nhom > 0" + (" AND active = 1" if active_only else "")
            + " GROUP BY nhom ORDER BY nhom")]


def set_sale_group(ids: list[int], nhom: int) -> list[int]:
    """Gán nhóm cho nhiều món (0 = bỏ nhóm). Trả về các id có thật đã đổi."""
    with connect() as conn:
        found = [i for i in ids if conn.execute("SELECT 1 FROM sales WHERE id = ?", (i,)).fetchone()]
        conn.executemany("UPDATE sales SET nhom = ? WHERE id = ?", [(nhom, i) for i in found])
    return found


def set_sale_stock(rec_id: int, link: str, result: dict) -> bool:
    """Lưu kết quả kiểm tra còn hàng của 1 link (ghi đè lần trước); bỏ kết quả của link đã xóa khỏi món sale."""
    with connect() as conn:
        r = conn.execute("SELECT link, ton_kho FROM sales WHERE id = ?", (rec_id,)).fetchone()
        if not r:
            return False
        links = (r["link"] or "").split()
        stock = {k: v for k, v in json.loads(r["ton_kho"] or "{}").items() if k in links}
        stock[link] = {**result, "at": _now()}
        conn.execute("UPDATE sales SET ton_kho = ? WHERE id = ?", (json.dumps(stock, ensure_ascii=False), rec_id))
        return True


def set_sale_variants(rec_id: int, link: str, names: list[str]) -> bool:
    """Phân loại cần mua của 1 link (web bấm 🎯); [] = bỏ chọn (đoán theo tên / tính tổng). Bỏ luôn link đã xóa."""
    with connect() as conn:
        r = conn.execute("SELECT link, phan_loai FROM sales WHERE id = ?", (rec_id,)).fetchone()
        if not r:
            return False
        links = (r["link"] or "").split()
        chosen = {k: v for k, v in json.loads(r["phan_loai"] or "{}").items() if k in links}
        if names:
            chosen[link] = names
        else:
            chosen.pop(link, None)
        conn.execute("UPDATE sales SET phan_loai = ? WHERE id = ?", (json.dumps(chosen, ensure_ascii=False), rec_id))
        return True


# ---------- tài khoản web ----------

WEB_ROLES = ("admin", "user")
SESSION_DAYS = 30


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), h)


def check_new_password(password: str):
    if len(password or "") < 6:
        raise ValueError("Mật khẩu cần ít nhất 6 ký tự")


def list_web_users():
    with connect() as conn:
        return conn.execute("SELECT id, username, role, active, created_at, last_login FROM web_users "
                            "ORDER BY role = 'admin' DESC, username").fetchall()


def get_web_user(user_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM web_users WHERE id = ?", (user_id,)).fetchone()


def find_web_user(username: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM web_users WHERE lower(username) = lower(?)", ((username or "").strip(),)).fetchone()


def create_web_user(username: str, password: str, role: str = "user") -> int:
    username = (username or "").strip()
    if not re.fullmatch(r"[\w.@-]{3,32}", username):
        raise ValueError("Tên đăng nhập 3–32 ký tự, chỉ chữ, số, . _ - @")
    if role not in WEB_ROLES:
        raise ValueError("Vai trò không hợp lệ")
    check_new_password(password)
    if find_web_user(username):
        raise ValueError(f"Đã có tài khoản “{username}”")
    with connect() as conn:
        return conn.execute("INSERT INTO web_users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?) "
                            "RETURNING id", (username, hash_password(password), role, _now())).fetchone()[0]


def _active_admins(conn, exclude_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM web_users WHERE role = 'admin' AND active = 1 AND id != ?",
                        (exclude_id,)).fetchone()[0]


def update_web_user(user_id: int, role: str | None = None, active: bool | None = None,
                    password: str | None = None):
    """Đổi vai trò / khóa-mở / đặt lại mật khẩu. Khóa hoặc đổi mật khẩu thì đăng xuất mọi phiên của người đó.
    Không cho làm mất admin cuối cùng."""
    with connect() as conn:
        u = conn.execute("SELECT * FROM web_users WHERE id = ?", (user_id,)).fetchone()
        if not u:
            raise ValueError("Không tìm thấy tài khoản")
        losing_admin = u["role"] == "admin" and u["active"] and (role == "user" or active is False)
        if losing_admin and not _active_admins(conn, user_id):
            raise ValueError("Phải còn ít nhất 1 admin đang hoạt động")
        if role is not None:
            if role not in WEB_ROLES:
                raise ValueError("Vai trò không hợp lệ")
            conn.execute("UPDATE web_users SET role = ? WHERE id = ?", (role, user_id))
        if active is not None:
            conn.execute("UPDATE web_users SET active = ? WHERE id = ?", (int(active), user_id))
        if password is not None:
            check_new_password(password)
            conn.execute("UPDATE web_users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
        if active is False or password is not None:
            conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))


def delete_web_user(user_id: int):
    with connect() as conn:
        u = conn.execute("SELECT * FROM web_users WHERE id = ?", (user_id,)).fetchone()
        if not u:
            raise ValueError("Không tìm thấy tài khoản")
        if u["role"] == "admin" and u["active"] and not _active_admins(conn, user_id):
            raise ValueError("Không xóa được admin cuối cùng")
        conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM web_users WHERE id = ?", (user_id,))


def web_user_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM web_users").fetchone()[0]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    with connect() as conn:
        conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (_now(),))
        conn.execute("INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                     (_token_hash(token), user_id, _now(),
                      (now + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")))
        conn.execute("UPDATE web_users SET last_login = ? WHERE id = ?", (_now(), user_id))
    return token


def session_user(token: str | None):
    """Tài khoản (đang hoạt động) của phiên còn hạn, None nếu không có."""
    if not token:
        return None
    with connect() as conn:
        return conn.execute(
            """SELECT u.* FROM web_sessions s JOIN web_users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1""",
            (_token_hash(token), _now())).fetchone()


def delete_session(token: str | None):
    if token:
        with connect() as conn:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_token_hash(token),))
