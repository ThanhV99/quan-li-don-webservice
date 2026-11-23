"""Giao diện web (FastAPI) dùng chung database với bot: xem / sửa đơn, săn sale, sản phẩm, người đặt, SĐT.

Chạy: uvicorn web:app --host 0.0.0.0 --port 8000 (docker compose: service "web").
Đăng nhập bằng tài khoản trong bảng web_users; tạo admin đầu tiên: python manage.py createadmin <tên>.
"""
import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import inspect
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import alert_rules
import db
import net
import rpc
import sale_text
import tracking
import utils
from utils import (addr_groups, money, money_short, parse_money, parse_pct, parse_range, pct_text, price_after_discount, range_short,
                   social_link)

log = logging.getLogger("web")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# ảnh QR tải lên từ web được gửi (im lặng, xóa ngay) vào chat admin đầu tiên để lấy file_id cho bot dùng chung
ADMIN_CHAT = next((int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x), None)
QR_MAX_BYTES = 10 * 1024 * 1024
COOKIE = "qld_session"
STATIC = Path(__file__).with_name("static")
WEB_USER_ID = 0  # created_by của bản ghi tạo từ web
PUBLIC_API = {"/api/login", "/api/setup", "/api/health"}
MERGE_PATH = re.compile(r"/api/people/\d+/merge")
# bot không có logic riêng: gọi hàm của web qua API (/api/svc/<module>.<hàm>, bot/remote.py) bằng token dịch vụ
# thay cho cookie đăng nhập
SERVICE_TOKEN = os.getenv("SERVICE_TOKEN", "")
SVC_PREFIX = "/api/svc/"
SVC_INFO = "__info__"  # <module>.__info__ → hằng số + tên hàm (+ hàm async / hàm thuần) cho bot
SVC_MODULES = {"db": db, "tracking": tracking, "sale_text": sale_text, "utils": utils, "alert_rules": alert_rules}
# db: chỉ những hàm bot.py dùng (không có tài khoản web, phiên đăng nhập, connect)
SVC_DB_FUNCS = frozenset("""
add_address add_carrier add_phone add_product add_product_image all_rows bot_heartbeat_age campaign_choices
campaign_name campaign_stats carrier_names customer_orders delete delete_carrier delete_person delete_phone
delete_product delete_product_image find_person find_product get get_alert_user get_carrier get_image get_meta get_phone
get_track_log insert insert_many list_addresses list_alert_users list_campaigns list_carriers list_orders
list_people list_phones list_products list_sales month_forecast month_orders_not_done month_status_counts
my_phones name_key name_taken norm_phone orders_by_phone orders_by_status person_defaults person_names person_open_orders
person_qr phone_by_number product_images product_images_map product_name_known product_picks product_price
remove_alert_user replace_file_id reserve_slot sale_groups save_image save_track_log search_orders sell_part set_all_sales_active split_all split_order
set_meta set_person_qr set_phone_mine set_phone_used set_product_price set_qr set_sale_group set_vc_alert
set_vc_near set_vc_next shop_names status_log sync_qr_images unfinished_logs update upsert_alert_user
""".split())
# hàm db CHỈ ĐỌC: bot được nhớ kết quả trong 1 lượt xử lý tin nhắn (remote.begin_request; lệnh ghi nào cũng xóa)
SVC_DB_READ = frozenset("""
all_rows bot_heartbeat_age campaign_choices campaign_name campaign_stats carrier_names customer_orders find_person
find_product get get_alert_user get_carrier get_image get_meta get_phone get_track_log list_addresses list_alert_users
list_campaigns list_carriers list_orders list_people list_phones list_products list_sales month_forecast
month_orders_not_done month_status_counts my_phones name_key name_taken norm_phone orders_by_phone orders_by_status
person_defaults person_names person_open_orders person_qr phone_by_number product_images product_images_map
product_name_known product_picks product_price sale_groups search_orders shop_names status_log unfinished_logs
""".split())
# hàm KHÔNG thuần của các module logic (đọc/ghi database, gọi hãng, theo giờ hiện tại); mọi hàm khác của
# tracking / sale_text / utils / alert_rules chỉ tính từ tham số → bot được nhớ kết quả (đỡ gọi lại money() …)
SVC_IMPURE = {"tracking": {"delivery_level", "with_saved_fallback", "relevel_order", "save_result",
                           "last_mile_summary", "throttle"}}
SVC_PURE_DB = {"name_key", "norm_phone"}
# module thuần bot TẢI MÃ NGUỒN về chạy tại chỗ (đỡ hàng trăm lượt gọi money() … qua mạng khi web ở Render): code chỉ
# nằm ở web; hằng số (kể cả lấy từ .env của web: VC_*, ADDR_ALIASES) bot ghi đè bằng giá trị bên web (__info__);
# hàm không thuần (database, gọi hãng, giờ hiện tại) vẫn gọi API. Thứ tự = thứ tự bot nạp (theo import).
SVC_SOURCE_MODULES = {"utils": utils, "alert_rules": alert_rules, "net": net, "sale_text": sale_text, "tracking": tracking}
SVC_SOURCE = "__source__"
SVC_CODE_VERSION = hashlib.sha256(b"".join(Path(m.__file__).read_bytes() for m in SVC_SOURCE_MODULES.values())).hexdigest()[:16]
rpc.register(tracking.TrackResult, tracking.TrackEvent)


def _svc_funcs(mod: str) -> dict:
    m = SVC_MODULES[mod]
    if mod == "db":
        return {n: getattr(db, n) for n in SVC_DB_FUNCS}
    return {n: f for n, f in vars(m).items()
            if not n.startswith("_") and inspect.isfunction(f) and f.__module__ == m.__name__}


SVC_FUNCS = {mod: _svc_funcs(mod) for mod in SVC_MODULES}


def _svc_info(mod: str) -> dict:
    consts = {}
    for name, val in vars(SVC_MODULES[mod]).items():
        if name.isupper() and not name.startswith("_"):
            try:
                rpc.encode(val)
            except TypeError:
                continue
            consts[name] = val
    funcs = SVC_FUNCS[mod]
    pure = (SVC_PURE_DB if mod == "db" else
            {n for n, f in funcs.items() if not inspect.iscoroutinefunction(f)} - SVC_IMPURE.get(mod, set()))
    local = {n: f.__module__ for n, f in funcs.items() if n in pure and f.__module__ in SVC_SOURCE_MODULES and mod == "db"}
    return {"consts": consts, "funcs": sorted(funcs), "pure": sorted(pure), "local": local, "code_version": SVC_CODE_VERSION,
            "read": sorted(SVC_DB_READ) if mod == "db" else [],
            "async": sorted(n for n, f in funcs.items() if inspect.iscoroutinefunction(f))}


# đổi mỗi lần deploy (giờ sửa index.html + web.py): trang đang mở thấy khác → báo tải lại (JS APP_VERSION)
APP_VERSION = str(int(max(STATIC.joinpath("index.html").stat().st_mtime, Path(__file__).stat().st_mtime)))

db.SOURCE = "web"
db.init_db()
if not alert_rules.PLACES_CONFIGURED:
    log.warning("Chưa đặt nơi nhận hàng trong .env (VC_ALERT_PLACES, VC_HUB_SPX / VC_HUB_JT / VC_HUB_GHN, VC_JT_AREA): "
                "bot chỉ báo theo 'Đang giao hàng', không biết đơn nào về kho cuối / sắp giao")
app = FastAPI(title="Quản lí đơn", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path.startswith(SVC_PREFIX):  # bot: chỉ token dịch vụ, không cookie, không đồng bộ ảnh QR mỗi lượt
        token = request.headers.get("X-Service-Token", "")
        if not SERVICE_TOKEN or not hmac.compare_digest(token.encode(), SERVICE_TOKEN.encode()):
            return JSONResponse({"detail": "Sai token dịch vụ"}, status_code=401)
        return await call_next(request)
    if path.startswith("/api/") and path not in PUBLIC_API:
        user = db.session_user(request.cookies.get(COOKIE))
        if not user:
            return JSONResponse({"detail": "Chưa đăng nhập"}, status_code=401)
        if path.startswith("/api/users") and user["role"] != "admin":
            return JSONResponse({"detail": "Chỉ admin mới quản lí tài khoản"}, status_code=403)
        # xóa (đơn, sale, sản phẩm, người đặt, SĐT, QR) và gộp người đặt: chỉ admin
        if user["role"] != "admin" and (request.method == "DELETE" or MERGE_PATH.fullmatch(path)):
            return JSONResponse({"detail": "Chỉ admin mới được xóa / gộp"}, status_code=403)
        request.state.user = user
        db.ACTOR.set(user["username"])  # nhật ký đổi trạng thái đơn ghi tài khoản web đã đổi
    resp = await call_next(request)
    resp.headers["X-App-Version"] = APP_VERSION
    # Vercel: bỏ thư mục ảnh QR theo tên (đĩa tạm, không ai xem) cho đỡ 1 lần đọc database mỗi lệnh ghi
    if request.method != "GET" and resp.status_code < 400 and path.startswith("/api/") and not os.getenv("VERCEL"):
        try:  # đổi QR / tên / gộp / xóa người đặt → cập nhật thư mục ảnh QR theo tên
            db.sync_qr_images()
        except OSError as e:
            log.warning("Không đồng bộ được thư mục ảnh QR: %s", e)
    return resp


@app.post(SVC_PREFIX + "{target}")
async def svc_call(target: str, request: Request):
    """Bot gọi 1 hàm: target = "<module>.<hàm>", body = rpc {args, kwargs, actor, source}. Lỗi của hàm trả về
    {"error": {type, message}} (HTTP 200) để bot ném lại đúng loại (ValueError ...)."""
    mod, _, fn = target.rpartition(".")
    if fn == SVC_SOURCE and mod in SVC_SOURCE_MODULES:
        source = Path(SVC_SOURCE_MODULES[mod].__file__).read_text(encoding="utf-8")
        return Response(rpc.dumps({"result": {"source": source, "code_version": SVC_CODE_VERSION}}),
                        media_type="application/json")
    if mod not in SVC_MODULES:
        return JSONResponse({"detail": f"Không có module {mod}"}, status_code=404)
    if fn == SVC_INFO:
        return Response(rpc.dumps({"result": _svc_info(mod)}), media_type="application/json")
    func = SVC_FUNCS[mod].get(fn)
    if func is None:
        return JSONResponse({"detail": f"Không có hàm {target}"}, status_code=404)
    body = rpc.loads(await request.body() or b"{}")
    db.ACTOR.set(body.get("actor"))
    db.SOURCE_CTX.set(body.get("source") or "bot")
    args, kwargs = body.get("args", ()), body.get("kwargs", {})
    try:
        if inspect.iscoroutinefunction(func):  # tra vận đơn: chạy trên vòng lặp của web
            result = await func(*args, **kwargs)
        else:  # to_thread chép context → ACTOR / SOURCE_CTX tới được db.update
            result = await asyncio.to_thread(func, *args, **kwargs)
        data = rpc.dumps({"result": result})
    except Exception as e:  # noqa: BLE001 – trả lỗi về bot
        if not isinstance(e, ValueError):
            log.exception("svc %s lỗi", target)
        data = rpc.dumps({"error": {"type": type(e).__name__, "message": str(e)}})
    return Response(data, media_type="application/json")


@app.exception_handler(ValueError)
async def bad_input(request: Request, e: ValueError):  # dữ liệu nhập sai → báo lỗi cho người dùng
    return JSONResponse({"detail": str(e)}, status_code=400)


# các tab của giao diện (static/index.html, ROUTES) – cùng 1 trang, JS đọc đường dẫn để mở đúng tab
@app.get("/")
@app.get("/don-hang")
@app.get("/san-sale")
@app.get("/san-pham")
@app.get("/nguoi-dat")
@app.get("/sdt")
@app.get("/dia-chi")
@app.get("/giao-hang")
@app.get("/tai-khoan")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__APP_VERSION__", APP_VERSION, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/icons/{name}")
def icon(name: str):  # icon sàn (static/icons/*.svg), không cần đăng nhập
    path = STATIC / "icons" / name
    if path.suffix != ".svg" or path.parent != STATIC / "icons" or not path.is_file():
        raise HTTPException(404)
    # trả thẳng nội dung (vài KB), không qua FileResponse → route công khai này không xử lý header Range
    return Response(path.read_bytes(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})


def me_out(u) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"]}


@app.get("/api/setup")
def setup():
    """Trang đăng nhập hỏi: đã có tài khoản nào chưa (chưa có → hướng dẫn lệnh tạo admin)."""
    return {"has_users": db.web_user_count() > 0}


@app.get("/api/health")
def health(bot: int = 1):
    """Không cần đăng nhập. 200 = web + database (+ bot, trừ khi ?bot=0) chạy tốt, 503 = có phần hỏng.
    Bot "ok" khi heartbeat trong meta mới hơn BOT_STALE_SECONDS."""
    out = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "web": "ok"}
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1 FROM orders LIMIT 1").fetchall()
        out["db"] = "ok"
        last, age = db.bot_heartbeat_age()
        out["bot"] = {"ok": age is not None and age <= db.BOT_STALE_SECONDS, "last_seen": last, "age_s": age}
    except Exception as e:  # noqa: BLE001
        log.warning("health: database lỗi: %s", e)
        out["db"], out["bot"] = f"lỗi: {type(e).__name__}", {"ok": False, "last_seen": None, "age_s": None}
    out["ok"] = out["db"] == "ok" and (out["bot"]["ok"] or not bot)
    return JSONResponse(out, status_code=200 if out["ok"] else 503)


# chống dò mật khẩu: đếm lần sai trong LOGIN_WINDOW giây theo tên đăng nhập và theo IP (bộ nhớ, 1 tiến trình uvicorn).
# Qua Docker mọi máy thường hiện cùng 1 IP (cổng bridge) nên giới hạn IP để cao hơn, tránh 1 người khóa cả nhà.
LOGIN_WINDOW, LOGIN_MAX_PER_USER, LOGIN_MAX_PER_IP = 15 * 60, 5, 20
_login_fails: dict[str, list[float]] = {}


def _recent_fails(key: str) -> list[float]:
    now = time.monotonic()
    fails = [t for t in _login_fails.get(key, []) if now - t < LOGIN_WINDOW]
    if fails:
        _login_fails[key] = fails
    else:
        _login_fails.pop(key, None)
    return fails


def _login_blocked(keys: list[tuple[str, int]]) -> int:
    """Số giây còn phải chờ nếu 1 khóa đã sai quá giới hạn, 0 nếu được thử."""
    wait = 0
    for key, limit in keys:
        fails = _recent_fails(key)
        if len(fails) >= limit:
            wait = max(wait, int(LOGIN_WINDOW - (time.monotonic() - fails[-limit])) + 1)
    return wait


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = str(body.get("username", ""))
    keys = [("u:" + username.strip().lower(), LOGIN_MAX_PER_USER),
            ("ip:" + (request.client.host if request.client else "?"), LOGIN_MAX_PER_IP)]
    if wait := _login_blocked(keys):
        log.warning("Chặn đăng nhập %r từ %s: sai quá nhiều lần", username, keys[1][0][3:])
        raise HTTPException(429, f"Đăng nhập sai quá nhiều lần, thử lại sau {(wait + 59) // 60} phút")
    u = db.find_web_user(username)
    # luôn băm mật khẩu kể cả khi sai tên để không đoán được tên nào tồn tại qua thời gian trả lời
    ok = db.check_password(str(body.get("password", "")), u["password_hash"] if u else db.hash_password("x"))
    if not u or not ok:
        for key, _ in keys:
            _login_fails.setdefault(key, []).append(time.monotonic())
        raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
    _login_fails.pop(keys[0][0], None)  # đúng mật khẩu → xóa đếm của tên này
    if not u["active"]:
        raise HTTPException(403, "Tài khoản đã bị khóa")
    resp = JSONResponse(me_out(u))
    resp.set_cookie(COOKIE, db.create_session(u["id"]), max_age=db.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return resp


@app.post("/api/logout")
def logout(request: Request):
    db.delete_session(request.cookies.get(COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(request: Request):
    return me_out(request.state.user)


@app.post("/api/me/password")
async def change_my_password(request: Request):
    body = await request.json()
    u = request.state.user
    if not db.check_password(str(body.get("old", "")), u["password_hash"]):
        raise ValueError("Mật khẩu hiện tại không đúng")
    db.update_web_user(u["id"], password=str(body.get("new", "")))  # đăng xuất mọi phiên → cấp phiên mới
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, db.create_session(u["id"]), max_age=db.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return resp


# ---------- quản lí tài khoản (admin) ----------

@app.get("/api/users")
def users():
    return rows(db.list_web_users())


@app.post("/api/users")
async def create_user(request: Request):
    body = await request.json()
    rec_id = db.create_web_user(str(body.get("username", "")), str(body.get("password", "")),
                                body.get("role") or "user")
    return {"id": rec_id}


@app.patch("/api/users/{user_id}")
async def update_user(user_id: int, request: Request):
    body = await request.json()
    if user_id == request.state.user["id"] and ("role" in body or body.get("active") is False):
        raise ValueError("Không tự khóa / hạ quyền chính mình")
    db.update_web_user(user_id, role=body.get("role"), active=body.get("active"),
                       password=body.get("password") or None)
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, request: Request):
    if user_id == request.state.user["id"]:
        raise ValueError("Không tự xóa chính mình")
    db.delete_web_user(user_id)
    return {"ok": True}


# ---------- chuyển dữ liệu ----------

def rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def qty(r) -> int:
    return max(int(r["so_luong"] or 1), 1)


def qr_version(file_id: str | None) -> str:
    """Đổi khi đổi ảnh QR → URL ảnh đổi theo, trình duyệt không hiện ảnh cũ đã nhớ."""
    return hashlib.sha256(file_id.encode()).hexdigest()[:8] if file_id else ""


def order_milestones(r, times: dict | None) -> dict:
    """Mốc giờ Đã nhận / Thanh toán / Hoàn thành của đơn (chỉ các bước đơn đang ở hoặc đã qua; đơn hủy: không có)."""
    st = r["trang_thai"]
    if st not in db.STATUS_FLOW or not times:
        return {}
    upto = db.STATUS_FLOW.index(st)
    return {s: times[s] for s in db.STATUS_FLOW[1:upto + 1] if s in times}


def order_out(r, times: dict | None = ...) -> dict:
    """times: {trạng thái: lúc} của đơn (danh sách truyền sẵn từ db.status_times(); bỏ trống = tự đọc 1 đơn)."""
    if times is ...:
        times = db.status_times(r["id"]).get(r["id"])
    d = dict(r)
    d["moc"] = order_milestones(r, times)
    d.pop("qr_file_id", None)
    d["has_qr"] = bool(r["qr_file_id"])
    d["next_statuses"] = db.next_statuses(r["trang_thai"])  # trạng thái được đổi sang (không nhảy cóc)
    d["qr_v"] = qr_version(r["qr_file_id"])
    d["thu"] = db.order_thu(r)  # giá shop thu + bonus thu
    d["margin"] = d["thu"] - ((r["gia_dat"] or 0) + (r["cong_dat"] or 0)) if r["gia_thu"] else None
    d["chien_dich_ten"] = db.campaign_name(r["chien_dich"]) if "chien_dich" in r.keys() else None
    d["trackable"] = tracking.is_supported(r["ma_don"], r["don_vi_vc"]) or bool(tracking.web_url(r["ma_don"], r["don_vi_vc"]))
    # GHN bắt captcha (11/2026) → không tra API được, tra qua tiện ích Chrome (nút 🧩 trên web)
    d["ghn"] = bool(r["ma_don"]) and tracking.is_ghn(r["ma_don"], r["don_vi_vc"])
    d["api_track"] = bool(r["ma_don"]) and tracking.is_supported(r["ma_don"], r["don_vi_vc"]) and not d["ghn"]
    # hành trình đã kết thúc (bảng track_logs) → hiện trạng thái cuối; cờ sắp giao / về kho cuối chỉ đúng khi đơn còn
    # Đã đặt (bot chỉ tra đơn Đã đặt, chuyển trạng thái rồi thì cờ không được cập nhật nữa)
    log = db.get_track_log(tracking.log_key(r["ma_don"])) if r["ma_don"] else None
    d["vc_status"] = log["milestone"] if log else ""  # trạng thái lần tra gần nhất
    d["vc_checked"] = log["updated_at"] if log else ""  # giờ lần tra được gần nhất (tra lỗi không ghi)
    d["vc_final"] = log["milestone"] if log and log["final"] else ""
    d["vc_delivered"] = bool(d["vc_final"]) and tracking.is_delivered(tracking.TrackResult("", "", True, milestone=d["vc_final"]))
    if r["trang_thai"] != db.STATUS_ORDERED or d["vc_final"]:
        d["vc_near"] = 0
    # đơn sắp giao: phân biệt 🏍 đang giao (mốc mới nhất = nhân viên đã nhận đi giao, kèm tên + SĐT nếu hãng ghi – J&T)
    # với 📮 sắp giao (mới tới bưu cục giao cuối) – như bot /danggiao
    d["vc_giao"], d["vc_courier"] = False, None
    if d["vc_near"] == 1 and log:
        res = tracking.result_from_dict(json.loads(log["data"]))
        if tracking.out_for_delivery(res):
            d["vc_giao"] = True
            if (c := tracking.courier_of(res)) and c["event"] is res.events[0]:
                d["vc_courier"] = {"name": c["name"], "phone": c["phone"], "post": c["post"],
                                   "time": c["time"].strftime("%H:%M %d/%m")}
    lo, hi = db.product_price(r["ten_sp"])
    d["gia_goi_y"] = range_short(lo, hi) if lo else ""
    return d


def sale_out(r) -> dict:
    d = dict(r)
    d["gia_label"] = range_short(r["gia_sale"], r["gia_sale_max"], "gia_sale") if r["gia_sale"] or r["gia_sale_max"] else ""
    d["cong_label"] = range_short(r["cong"], r["cong_max"], "cong") if r["cong"] or r["cong_max"] else ""
    d["links"] = (r["link"] or "").split()
    d["gia_goc_label"] = money_short(r["gia_goc"]) if r["gia_goc"] else ""
    d["giam_toi_da_label"] = money_short(r["giam_toi_da"]) if r["giam_toi_da"] else ""
    d["pct"] = sale_text.sale_pct(r, r["gia_goc"])
    d["tiers"] = [{**t, "pct": sale_text.sale_pct(t, r["gia_goc"]),
                   "cap_label": money_short(t["giam_toi_da"]) if t["giam_toi_da"] else "",
                   "gia_label": range_short(t["gia_sale"], t["gia_sale_max"], "gia_sale"),
                   "cong_label": range_short(t["cong"], t["cong_max"], "cong") if t["cong"] or t["cong_max"] else ""}
                  for t in sale_text.tiers_of(r)]
    stock = json.loads(r["ton_kho"] or "{}")
    d["stock"] = {u: stock[u] for u in d["links"] if u in stock}  # kết quả kiểm tra còn hàng theo link
    # phân loại cần mua: chọn 🎯 / đoán theo tên / tổng → còn hàng tính theo đó (sale_text.stock_view, như bot /sale)
    chosen = json.loads(r["phan_loai"] or "{}")
    d["variants"] = {u: chosen[u] for u in d["links"] if u in chosen}
    text = f"{r['ten_sp'] or ''} {r['shop'] or ''}"
    d["stock_view"] = {u: v for u in d["stock"] if (v := sale_text.stock_view(d["stock"][u], chosen.get(u), text))}
    del d["ton_kho"], d["phan_loai"]
    return d


def product_out(r) -> dict:
    d = dict(r)
    d["gia_label"] = range_short(r["gia_goi_y"], r["gia_goi_y_max"]) if r["gia_goi_y"] else ""
    return d


def clean(v):
    v = v.strip() if isinstance(v, str) else v
    return None if v in ("", None) else v


def convert(table: str, field: str, raw):
    """Giá trị gõ trên web → giá trị lưu DB (tiền đọc như bot: 4tr3, 150k, 150-200k). Khoảng giá → (thấp, cao)."""
    raw = clean(raw)
    if field in db.RANGE_FIELDS:
        if raw is None:
            return (0, None)
        got = parse_range(str(raw))
        if not got:
            raise ValueError(f"Không đọc được giá: {raw}")
        return got
    if field in db.MONEY_FIELDS:
        if raw is None:
            return 0
        got = parse_money(str(raw))
        if got is None:
            raise ValueError(f"Không đọc được số tiền: {raw}")
        return got
    if field == "uu_tien":  # thứ tự 1, 2, 3…; trống / 0 = không xếp
        if not str(raw or 0).isdigit():
            raise ValueError("Thứ tự ưu tiên phải là số: 1, 2, 3… (để trống = xếp sau cùng)")
        return int(raw or 0)
    if field == "chien_dich":  # id chiến dịch thưởng săn sale; trống = không thuộc chiến dịch
        if raw in (None, "", 0, "0"):
            return None
        if not str(raw).isdigit() or not db.get_campaign(int(raw)):
            raise ValueError("Không có chiến dịch này")
        return int(raw)
    if field == "nhom":  # nhóm sale 1–99 (/sale 1); trống / 0 = chưa chia nhóm
        if not str(raw or 0).isdigit() or int(raw or 0) > 99:
            raise ValueError("Nhóm sale phải là số 1 – 99 (để trống = chưa chia nhóm)")
        return int(raw or 0)
    if field == "so_luong":
        if raw is None:
            return None
        if not str(raw).isdigit():
            raise ValueError("Số lượng phải là số")
        return int(raw)
    if field in ("zalo", "facebook"):
        return social_link(field, raw)
    if field == "giam_pt":
        if raw is None:
            return None
        value = parse_pct(str(raw) if "%" in str(raw) else f"{raw}%")
        if not value:
            raise ValueError("Phần trăm giảm phải từ 0 đến 100, ví dụ: 10 hoặc 12.5")
        return value
    if field == "ha_noi":
        return None if raw in (None, "") else (1 if raw in (1, True, "1", "true") else 0)
    if field == "muc_gia":
        value, errors = sale_text.parse_tiers(raw)
        if errors:
            raise ValueError("Không đọc được mức giá: " + "; ".join(errors) + " (vd: dưới 4tr5 công 100)")
        return value
    if field in ("active", "da_su_dung", "cua_toi"):
        return 1 if raw in (1, True, "1", "true") else 0
    if field == "trang_thai" and raw not in db.STATUSES:
        raise ValueError("Trạng thái không hợp lệ")
    if field == "noi_dung" and raw is None:
        raise ValueError("Địa chỉ không được để trống")
    if field in ("ten", "ten_nguoi_dat") and raw is None:
        raise ValueError("Tên không được để trống")
    return raw


def apply_update(table: str, rec_id: int, field: str, raw):
    """Sửa 1 trường, giữ đúng luật của bot (giá shop thu nhập theo 1 cái, Hoàn thành cần giá thu…)."""
    r = db.get(table, rec_id)
    if not r:
        raise HTTPException(404, "Không tìm thấy")
    if field not in dict(db.TABLES[table]):
        raise ValueError(f"Trường không sửa được: {field}")
    value = convert(table, field, raw)
    if field == "ten" and table in ("products", "nguoi_dat", "carriers") and db.name_taken(table, rec_id, value):
        raise ValueError(f"Đã có tên “{value}”")
    if table == "orders" and field == "trang_thai" and (err := db.status_change_error(r["trang_thai"], value)):
        raise ValueError(err)
    if table == "orders" and field == "trang_thai" and value == db.STATUS_DONE and not r["gia_thu"]:
        raise ValueError("Đơn chưa có giá shop thu – dùng nút Bán / Hoàn thành")
    if field in db.RANGE_FIELDS:
        db.update(table, rec_id, db.RANGE_FIELDS[field], value[1])
        value = value[0]
    if table == "orders" and field == "gia_thu" and value:
        value *= qty(r)  # nhập giá 1 cái → lưu tổng cả đơn
    db.update(table, rec_id, field, value)
    if table == "orders" and field == "gia_thu" and not value and r["trang_thai"] == db.STATUS_DONE:
        db.update(table, rec_id, "trang_thai", db.STATUS_PAID)


# ---------- danh mục chung ----------

@app.get("/api/meta")
def meta():
    return {
        "statuses": db.STATUSES,
        "people": db.person_names(),
        "campaigns": db.campaign_choices(),  # [(id, tên)] chọn chiến dịch thưởng cho đơn
        "products": db.product_names(),
        "product_prices": db.product_prices(),
        "carriers": db.carrier_names(),
        "shops": db.shop_names(30),
        "nguon_khach": ["Zalo", "Facebook"],
        "nguon_don": ["Shopee", "TikTok"],
        "fields": {t: dict(f) for t, f in db.TABLES.items()},
    }


@app.get("/api/stats")
def stats(month: str | None = None):
    month = month or datetime.now().strftime("%Y-%m")
    by_src = rows(db.month_stats(month))
    total = {k: sum(r[k] for r in by_src) for k in ("so_don", "tong_dat", "tong_cong", "tong_thu")}
    done = [r for r in by_src if r["hoan_thanh"]]
    with db.connect() as conn:
        counts = dict(conn.execute("SELECT trang_thai, COUNT(*) FROM orders GROUP BY trang_thai").fetchall())
        months = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(created_at, 1, 7) FROM orders ORDER BY 1 DESC").fetchall()]
        daily = rows(conn.execute(
            """SELECT substr(created_at, 9, 2) AS ngay, COUNT(*) AS so_don FROM orders
               WHERE created_at LIKE ? AND trang_thai != ? GROUP BY ngay ORDER BY ngay""",
            (month + "%", db.STATUS_CANCELED)).fetchall())
        # top công của tháng theo người đặt (không tính đơn hủy)
        top_cong = rows(conn.execute(
            """SELECT MIN(ten_nguoi_dat) AS ten, COUNT(*) AS so_don, SUM(cong_dat) AS cong, MAX(cong_dat) AS cao_nhat
               FROM orders WHERE created_at LIKE ? AND trang_thai != ? AND cong_dat > 0
               GROUP BY name_key(ten_nguoi_dat) ORDER BY cong DESC LIMIT 5""",
            (month + "%", db.STATUS_CANCELED)).fetchall())
    people = rows(db.list_people())
    everything = db.month_forecast()
    fc = db.month_forecast(month)
    thuong = sum(r["thuong"] for r in fc.values())  # thưởng chiến dịch của các đơn mốc trong tháng
    return {
        "month": month,
        "months": months if month in months else [month, *months],
        "total": total,
        "spent": total["tong_dat"] + total["tong_cong"] + thuong,
        "thuong": thuong,
        "margin_done": fc[db.STATUS_DONE]["lai"],
        "done_count": sum(r["so_don"] for r in done),
        "by_source": by_src,
        "daily": daily,
        "status_counts": {s: counts.get(s, 0) for s in db.STATUSES},
        "month_counts": db.month_status_counts(month),
        "unpaid": [{"ten": p["ten"], "so_dat": p["so_dat"], "so_chua_tra": p["so_chua_tra"], "cong": p["cong_chua_tra"]}
                   for p in people if p["cong_chua_tra"]],
        "unsold": len(db.month_orders_not_done(month)),
        "forecast": fc,
        # vốn đã bỏ ra chưa thu về: mọi tháng, đơn chưa bán
        "outstanding": {st: everything[st] for st in db.UNSOLD_STATUSES},
        "top_cong": top_cong,
    }


# ---------- đơn hàng ----------

@app.get("/api/orders")
def orders(status: str | None = None, q: str | None = None, tu: str | None = None, den: str | None = None,
           cd: str | None = None, vc: str | None = None, san: str | None = None, ngay: str | None = None,
           dc: str | None = None, limit: int = 300):
    """Đơn mới nhất theo trạng thái ('all' = cả đơn hủy, bỏ trống = trừ đơn hủy), từ khóa, ngày tạo tu..den
    (YYYY-MM-DD, tính cả 2 đầu), đơn vị vận chuyển vc (tên; "-" = chưa có ĐVVC), sàn san (nguon_don; "-" = chưa ghi).
    Kèm số đơn từng trạng thái theo cùng bộ lọc, số đơn từng ĐVVC (bộ lọc + trạng thái + sàn, chưa lọc vc)
    và từng sàn (bộ lọc + trạng thái + vc, chưa lọc sàn). cd = id chiến dịch thưởng, "-" = đơn không gán chiến dịch;
    cds = số đơn từng chiến dịch (bộ lọc + trạng thái + vc + sàn, chưa lọc cd).
    dc = khóa nhóm địa chỉ (addr_groups: địa chỉ giống nhau gom 1 nhóm, "-" = chưa có địa chỉ);
    addresses = tổng theo nhóm địa chỉ của các đơn khớp mọi bộ lọc trừ dc."""
    # nhóm địa chỉ tính trên mọi đơn để khóa nhóm không đổi theo bộ lọc
    groups = addr_groups(r["dia_chi"] for r in db.list_orders(limit=None) + db.list_orders(limit=None, canceled=True))
    grp = lambda r: groups.get((r["dia_chi"] or "").strip(), ("-", ""))
    dc_ok = (lambda r: True) if not dc else (lambda r: grp(r)[0] == dc)
    cd_ok = (lambda r: True) if not cd else (lambda r: not r["chien_dich"]) if cd == "-" else \
        (lambda r: str(r["chien_dich"] or "") == cd)
    rs = db.search_orders(q, -1) if q else db.list_orders(limit=None) + db.list_orders(limit=None, canceled=True)
    # ngay=tt: tu..den lọc theo ngày thanh toán công (mốc chuyển sang Thanh toán trong nhật ký), không theo ngày tạo
    times = db.status_times()
    paid = {i: t[db.STATUS_PAID] for i, t in times.items() if db.STATUS_PAID in t}
    by_paid = ngay == "tt"
    no_mark = 0  # đơn đã trả công nhưng trả trước khi có nhật ký → không lọc theo ngày thanh toán được
    since = (db.get_meta(db.STATUS_LOG_SINCE_KEY) or "")[:10]
    if by_paid and (tu or den):
        # chỉ đáng báo khi khoảng ngày bắt đầu trước lúc có nhật ký (đơn trả công sau đó luôn có mốc)
        if not tu or not since or tu <= since:
            no_mark = sum(1 for r in rs if r["id"] not in paid and r["trang_thai"] in (db.STATUS_PAID, db.STATUS_DONE)
                          and cd_ok(r))
        rs = [r for r in rs if r["id"] in paid]
    day = (lambda r: paid[r["id"]][:10]) if by_paid else (lambda r: r["created_at"][:10])
    if tu:
        rs = [r for r in rs if day(r) >= tu]
    if den:
        rs = [r for r in rs if day(r) <= den]

    def status_ok(r):
        if status == "all":
            return True
        return r["trang_thai"] == status if status else r["trang_thai"] != db.STATUS_CANCELED
    def facet(col: str, want: str | None):
        """Hàm r → đơn r khớp bộ lọc want của cột col (tên, "-" = trống, rỗng = mọi giá trị)."""
        key = None if not want else "" if want == "-" else db.name_key(want)
        return lambda r: key is None or db.name_key((r[col] or "").strip()) == key

    vc_ok, san_ok = facet("don_vi_vc", vc), facet("nguon_don", san)

    def count_by(col: str, rows) -> list[dict]:
        out: dict[str, dict] = {}
        for r in rows:
            name = (r[col] or "").strip()
            out.setdefault(db.name_key(name), {"ten": name, "don": 0})["don"] += 1
        return sorted(out.values(), key=lambda c: (not c["ten"], -c["don"], c["ten"]))

    carriers = count_by("don_vi_vc", [r for r in rs if status_ok(r) and san_ok(r) and cd_ok(r) and dc_ok(r)])
    sans = count_by("nguon_don", [r for r in rs if status_ok(r) and vc_ok(r) and cd_ok(r) and dc_ok(r)])
    # số đơn từng chiến dịch (chiến dịch đang bật luôn có mặt, kể cả 0 đơn); id "" = không gán chiến dịch
    camps = {c["id"]: c for c in db.list_campaigns()}
    cd_n: dict[int | None, int] = {c: 0 for c, v in camps.items() if v["active"]}
    for r in rs:
        if status_ok(r) and vc_ok(r) and san_ok(r) and dc_ok(r):
            cd_n[r["chien_dich"] or None] = cd_n.get(r["chien_dich"] or None, 0) + 1
    cds = sorted(({"id": c or "", "ten": camps[c]["ten"] if c in camps else ("" if not c else f"chiến dịch #{c}"),
                   "active": bool(camps[c]["active"]) if c in camps else False, "don": n} for c, n in cd_n.items()),
                 key=lambda c: (not c["id"], not c["active"], -c["don"], c["ten"]))
    rs = [r for r in rs if vc_ok(r) and san_ok(r) and cd_ok(r)]
    # tổng theo nhóm địa chỉ (bộ lọc + trạng thái, chưa lọc dc): số đơn, giá đặt, các cách viết, nhiều đơn lên đầu
    addresses: dict[str, dict] = {}
    for r in rs:
        if not status_ok(r):
            continue
        key, label = grp(r)
        a = addresses.setdefault(key, {"key": key, "ten": label, "don": 0, "gia_dat": 0, "cach_viet": {}})
        a["don"] += 1
        a["gia_dat"] += r["gia_dat"] or 0
        raw = (r["dia_chi"] or "").strip()
        if raw:
            a["cach_viet"][raw] = a["cach_viet"].get(raw, 0) + 1
    for a in addresses.values():
        a["cach_viet"] = sorted(a["cach_viet"], key=lambda x: -a["cach_viet"][x])
    rs = [r for r in rs if dc_ok(r)]
    counts = {st: sum(r["trang_thai"] == st for r in rs) for st in db.STATUSES}
    rs = [r for r in rs if status_ok(r)]
    rs.sort(key=lambda r: -r["id"])
    # tổng theo người đặt của mọi đơn khớp bộ lọc (không chỉ 300 đơn đang hiện), giá đặt nhiều lên đầu
    people: dict[str, dict] = {}
    for r in rs:
        p = people.setdefault(db.name_key(r["ten_nguoi_dat"] or ""), {"ten": r["ten_nguoi_dat"] or "", "don": 0, "gia_dat": 0})
        p["don"] += 1
        p["gia_dat"] += r["gia_dat"] or 0
    # tổng theo sản phẩm: số đơn, số lượng (cộng so_luong), giá đặt; nhiều cái lên đầu
    products: dict[str, dict] = {}
    for r in rs:
        p = products.setdefault(db.name_key(r["ten_sp"] or ""), {"ten": r["ten_sp"] or "", "don": 0, "sl": 0, "gia_dat": 0})
        p["don"] += 1
        p["sl"] += r["so_luong"] or 1
        p["gia_dat"] += r["gia_dat"] or 0
    if by_paid:  # mới thanh toán trước
        rs.sort(key=lambda r: paid.get(r["id"], ""), reverse=True)
    seq = db.campaign_order_seq()
    out = []
    for r in rs[:limit]:
        o = order_out(r, times.get(r["id"]))
        o["paid_at"] = paid.get(r["id"])
        o["cd_seq"] = seq.get(r["id"])  # đơn thứ mấy của người đặt trong chiến dịch, có phải đơn đạt mốc thưởng
        o["dc_key"], o["dc_ten"] = grp(r)
        out.append(o)
    return {"orders": out, "counts": counts, "total": len(rs), "no_paid_mark": no_mark,
            "status_log_since": since if no_mark else None,
            "people": sorted(people.values(), key=lambda p: (-p["gia_dat"], -p["don"])),
            "products": sorted(products.values(), key=lambda p: (-p["sl"], -p["gia_dat"])),
            "carriers": carriers, "sans": sans, "cds": cds,
            "addresses": sorted(addresses.values(), key=lambda a: (a["key"] == "-", -a["don"], -a["gia_dat"]))}


@app.get("/api/orders/{rec_id}")
def order(rec_id: int):
    r = db.get("orders", rec_id)
    if not r:
        raise HTTPException(404, "Không tìm thấy đơn")
    # các mốc đổi trạng thái (chỉ ở chi tiết 1 đơn); status_log_since = từ lúc nào mới có nhật ký
    return {**order_out(r), "cd_seq": db.campaign_order_seq().get(rec_id),
            "status_log": [dict(g) for g in db.status_log(rec_id)],
            "status_log_since": db.get_meta(db.STATUS_LOG_SINCE_KEY)}


@app.post("/api/orders")
async def create_order(request: Request):
    body = await request.json()
    data = {}
    for field, _ in db.ORDER_FIELDS:
        if field in ("trang_thai",) or field not in body:
            continue
        value = convert("orders", field, body[field])
        if value not in (None, 0, ""):
            data[field] = value
    if not data.get("ten_nguoi_dat"):
        raise ValueError("Cần tên người đặt")
    if data.get("gia_thu"):
        data["gia_thu"] *= max(data.get("so_luong") or 1, 1)
    rec_id = db.insert("orders", data, WEB_USER_ID)
    return order_out(db.get("orders", rec_id))


# ---------- chiến dịch thưởng săn sale (đặt trước route chung /api/{table}/{rec_id}) ----------

def campaign_in(body: dict) -> dict:
    """Form chiến dịch → dữ liệu lưu: ngày YYYY-MM-DD, tiền gõ như bot (100k, 100)."""
    ten = clean(body.get("ten"))
    if not ten:
        raise ValueError("Nhập tên chiến dịch")
    tu, den = clean(body.get("tu")), clean(body.get("den"))
    for d in (tu, den):
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise ValueError(f"Ngày không đúng: {d}")
    if not tu:
        raise ValueError("Nhập ngày bắt đầu")
    if den and den < tu:
        raise ValueError("Ngày kết thúc phải sau ngày bắt đầu")
    moc = str(clean(body.get("moc_don")) or "")
    if not moc.isdigit() or int(moc) < 1:
        raise ValueError("Mốc đơn phải là số ≥ 1, vd 5")
    thuong = parse_money(str(clean(body.get("thuong")) or ""))
    if not thuong:
        raise ValueError("Nhập tiền thưởng, vd 100k")
    active = 0 if body.get("active") in (0, "0", False, "false") else 1
    return {"ten": ten, "tu": tu, "den": den, "moc_don": int(moc), "thuong": thuong, "active": active}


def campaign_out(c) -> dict:
    return {**dict(c), **db.campaign_stats(c), "thuong_label": money_short(c["thuong"])}


@app.get("/api/campaigns")
def campaigns():
    return [campaign_out(c) for c in db.list_campaigns()]


@app.post("/api/campaigns")
async def create_campaign(request: Request):
    return campaign_out(db.get_campaign(db.save_campaign(None, campaign_in(await request.json()))))


@app.patch("/api/campaigns/{cid}")
async def update_campaign(cid: int, request: Request):
    if not db.get_campaign(cid):
        raise HTTPException(404, "Không tìm thấy chiến dịch")
    return campaign_out(db.get_campaign(db.save_campaign(cid, campaign_in(await request.json()))))


@app.post("/api/campaigns/{cid}/active")
async def campaign_active(cid: int, request: Request):
    """Bật / tắt chiến dịch (tắt = không hiện để chọn cho đơn mới, bảng thưởng vẫn giữ)."""
    if not db.set_campaign_active(cid, (await request.json()).get("active")):
        raise HTTPException(404, "Không tìm thấy chiến dịch")
    return campaign_out(db.get_campaign(cid))


@app.delete("/api/campaigns/{cid}")
def delete_campaign(cid: int):
    if not db.delete_campaign(cid):
        raise HTTPException(404, "Không tìm thấy chiến dịch")
    return {"ok": True}


# ---------- ảnh sản phẩm (bot /anhsp, /sale gửi kèm) — đăng ký trước route chung /api/{table}/{rec_id} ----------

@app.post("/api/product-images")
async def add_product_image(request: Request, name: str = ""):
    """Body = 1 ảnh; ?name= tên sản phẩm / món sale (gửi nhiều ảnh = gọi nhiều lần)."""
    ten = db.product_name_known(name)
    if not ten:
        raise HTTPException(404, "Không có sản phẩm / món sale này")
    if len(db.product_images(ten)) >= db.MAX_PRODUCT_IMAGES:
        raise ValueError(f"Đã đủ {db.MAX_PRODUCT_IMAGES} ảnh (1 album Telegram)")
    data, ctype = await read_image(request)
    file_id = await store_upload(data, ctype)
    if (img_id := db.add_product_image(ten, file_id)) is None:
        raise ValueError(f"Đã đủ {db.MAX_PRODUCT_IMAGES} ảnh (1 album Telegram)")
    return {"id": img_id}


@app.get("/api/product-images/{img_id}")
async def product_image(img_id: int):
    r = db.get_product_image(img_id)
    if not r:
        raise HTTPException(404, "Không có ảnh")
    return Response(await image_bytes(r["file_id"]), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=604800"})


@app.delete("/api/product-images/{img_id}")
def delete_product_image(img_id: int):
    if not db.delete_product_image(img_id):
        raise HTTPException(404, "Không có ảnh")
    return {"ok": True}


@app.patch("/api/{table}/{rec_id}")
async def patch(table: str, rec_id: int, request: Request):
    if table not in db.TABLES:
        raise HTTPException(404)
    body = await request.json()
    for field, raw in body.items():
        apply_update(table, rec_id, field, raw)
    r = db.get(table, rec_id)
    return {"orders": order_out, "sales": sale_out, "products": product_out}.get(table, dict)(r)


@app.delete("/api/{table}/{rec_id}")
def remove(table: str, rec_id: int):
    if table not in db.TABLES:
        raise HTTPException(404)
    if not db.delete(table, rec_id):
        raise HTTPException(404, "Không tìm thấy")
    return {"ok": True}


@app.post("/api/orders/{rec_id}/split")
async def split(rec_id: int, request: Request):
    """Tách đơn nhiều cái (không bán): {"qty": k} = tách k cái ra đơn mới; {"all": true} = tách hết thành từng đơn 1 cái.
    Giá đặt / công / giá shop thu chia theo số cái (db.split_order)."""
    body = await request.json()
    ids = db.split_all(rec_id) if body.get("all") else [db.split_order(rec_id, int(body.get("qty") or 0))]
    return {"order": order_out(db.get("orders", rec_id)), "new_ids": ids}


@app.post("/api/orders/{rec_id}/sell")
async def sell(rec_id: int, request: Request):
    """Bán xong: nhập giá shop thu 1 cái → lưu tổng, chuyển Hoàn thành (như /ban). qty < số lượng của đơn = bán
    1 phần: phần còn lại tách thành đơn mới (db.sell_part)."""
    body = await request.json()
    r = db.get("orders", rec_id)
    if not r:
        raise HTTPException(404, "Không tìm thấy đơn")
    if err := db.status_change_error(r["trang_thai"], db.STATUS_DONE):
        raise ValueError(err)
    price = convert("orders", "gia_thu", body.get("price"))
    sell_qty = int(body.get("qty") or 0)
    if sell_qty and not 1 <= sell_qty <= qty(r):
        raise ValueError(f"Đơn #{rec_id} có {qty(r)} cái – số cái bán phải từ 1 tới {qty(r)}")
    if 0 < sell_qty < qty(r):
        new_id = db.sell_part(rec_id, sell_qty, price)
        return {**order_out(db.get("orders", rec_id)), "tach": new_id}
    if price:
        db.update("orders", rec_id, "gia_thu", price * qty(r))
    elif not r["gia_thu"]:
        raise ValueError("Nhập giá shop thu")
    db.update("orders", rec_id, "trang_thai", db.STATUS_DONE)
    return order_out(db.get("orders", rec_id))


@app.get("/api/orders/{rec_id}/track")
async def track(rec_id: int, saved: int = 0):
    """saved=1: chỉ đọc hành trình đã lưu (track_logs), không gọi API hãng.
    Giãn cách giữa các lần gọi API hãng do tracking.throttle lo (chung với bot, khe giờ trong bảng meta)."""
    r = db.get("orders", rec_id)
    if not r or not r["ma_don"]:
        raise HTTPException(404, "Đơn chưa có mã vận đơn")
    if saved:
        row = db.get_track_log(tracking.log_key(r["ma_don"]))
        if row:
            res = tracking.result_from_dict(json.loads(row["data"]))
            res.saved_at = row["updated_at"] or ""
        else:
            res = tracking.TrackResult(r["don_vi_vc"] or "", r["ma_don"], False, error="Chưa có hành trình lưu")
        res.web_url = res.web_url or tracking.site_url(r["ma_don"], r["don_vi_vc"])
        ghn = tracking.is_ghn(r["ma_don"], r["don_vi_vc"])
        return {**_track_out(res), "final": bool(row and row["final"]), "from_db": True, "ghn": ghn,
                "api_track": tracking.is_supported(r["ma_don"], r["don_vi_vc"]) and not ghn}
    res = await tracking.track_saved(r["ma_don"], r["don_vi_vc"], r["sdt"])
    tracking.relevel_order(r, res)  # cờ sắp giao + kéo giờ tra của bot lên sớm (alert vẫn do bot gửi)
    return _track_out(tracking.with_saved_fallback(res))


@app.post("/api/orders/{rec_id}/ghn-data")
async def ghn_data(rec_id: int, request: Request):
    """Hành trình GHN do tiện ích Chrome đọc trên donhang.ghn.vn (API GHN bắt captcha, người dùng tự giải trong
    Chrome) → lưu track_logs + cập nhật cờ sắp giao như lần tra API; alert do bot watcher gửi (dùng bản lưu)."""
    r = db.get("orders", rec_id)
    if not r or not r["ma_don"]:
        raise HTTPException(404, "Đơn chưa có mã vận đơn")
    if not tracking.is_ghn(r["ma_don"], r["don_vi_vc"]):
        raise HTTPException(400, "Đơn này không phải GHN")
    body = await request.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise HTTPException(400, "Thiếu dữ liệu hành trình")
    got = str((data.get("order_info") or {}).get("order_code") or "").strip().upper()
    if got and got != tracking.log_key(r["ma_don"]):
        raise HTTPException(400, f"Hành trình của mã {got}, không phải {r['ma_don']}")
    res = tracking.parse_ghn(r["ma_don"], data)
    if not res.events:
        raise HTTPException(400, "Hành trình trống")
    tracking.save_result(res, r["ma_don"])
    tracking.relevel_order(r, res)
    return _track_out(res)


def _track_out(res) -> dict:
    return {
        "saved_at": res.saved_at, "stale": res.stale,
        "carrier": res.carrier, "code": res.tracking_no, "ok": res.ok, "error": res.error,
        "milestone": res.milestone, "web_url": res.web_url,
        "eta": res.eta.strftime("%d/%m/%Y") if res.eta else None,
        "events": [{"time": e.time.strftime("%d/%m %H:%M"), "text": e.description, "location": e.location}
                   for e in res.events],
    }


# ảnh (QR, ảnh sản phẩm) lưu trong database (db.image_files): bot lưu ngay khi nhận ảnh; ảnh còn thiếu (nằm ở thư mục
# đĩa cũ data/qr_cache, hoặc bot lỡ chưa lưu) thì web tự chép / tải từ Telegram rồi lưu vào database
QR_PREFETCH_MINUTES = 10
_image_locks: dict[str, asyncio.Lock] = {}


async def image_bytes(file_id: str) -> bytes:
    """Ảnh theo file_id: lấy trong database; chưa có thì chép từ thư mục đĩa cũ (trước 11/2026) hoặc tải từ Telegram
    (getFile + tải file) bằng BOT_TOKEN, rồi lưu vào database."""
    if (data := db.get_image(file_id)) is not None:
        return data
    async with _image_locks.setdefault(file_id, asyncio.Lock()):  # 2 request cùng lúc chỉ tải 1 lần
        if (data := db.get_image(file_id)) is not None:
            return data
        legacy = Path(db.legacy_cache_path(file_id))
        if legacy.exists():
            data = legacy.read_bytes()
        else:
            if not BOT_TOKEN:
                raise HTTPException(404, "Database chưa có ảnh này (thiếu BOT_TOKEN để tải từ Telegram)")
            api = f"https://api.telegram.org/bot{BOT_TOKEN}"
            info = (await net.request("GET", f"{api}/getFile", params={"file_id": file_id})).json()
            if not info.get("ok"):
                raise HTTPException(404, "Không tải được ảnh từ Telegram")
            data = (await net.request("GET", f"https://api.telegram.org/file/bot{BOT_TOKEN}/"
                                             f"{info['result']['file_path']}", timeout=30)).content
        db.save_image(file_id, data)
    _image_locks.pop(file_id, None)
    return data


async def prefetch_qr_loop():
    """Mỗi QR_PREFETCH_MINUTES phút: lưu vào database ảnh còn thiếu của mọi QR / ảnh sản phẩm đang dùng, xóa ảnh
    không còn ai dùng, cập nhật thư mục ảnh QR theo tên (cả thay đổi làm qua bot: đổi tên, xóa người…)."""
    while True:
        try:
            for file_id in db.missing_images():
                try:
                    await image_bytes(file_id)
                except Exception as e:  # noqa: BLE001 – mạng lỗi → lần sau tải lại
                    log.warning("Không lấy được ảnh %s…: %s", file_id[:16], getattr(e, "detail", e))
            if n := db.prune_images():
                log.info("Đã xóa %d ảnh không còn dùng", n)
            db.sync_qr_images()
        except Exception as e:  # noqa: BLE001 – database / đĩa lỗi: vòng sau thử lại
            log.warning("Vòng đồng bộ ảnh lỗi: %s", e)
        await asyncio.sleep(QR_PREFETCH_MINUTES * 60)


@app.on_event("startup")
async def start_prefetch():
    # Vercel (serverless, tự đặt VERCEL=1) không chạy nền liên tục: bỏ vòng lặp, ảnh vẫn tải khi có người xem
    if os.getenv("VERCEL"):
        return
    asyncio.create_task(prefetch_qr_loop())


QR_TABLES = {"order": "orders", "person": "nguoi_dat"}


async def upload_qr(data: bytes, content_type: str) -> str:
    """Gửi ảnh qua bot chính vào chat admin để Telegram cấp file_id (bot dùng được như ảnh gửi trực tiếp),
    rồi xóa tin đó. Trả về file_id."""
    if not BOT_TOKEN or not ADMIN_CHAT:
        raise ValueError("Thiếu BOT_TOKEN / ALLOWED_USER_IDS trong .env")
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    resp = await net.request("POST", f"{api}/sendPhoto", timeout=60,
                             data={"chat_id": ADMIN_CHAT, "disable_notification": "true"},
                             files={"photo": ("qr" + (".png" if "png" in content_type else ".jpg"), data, content_type)})
    res = resp.json()
    if not res.get("ok"):
        raise ValueError(f"Telegram không nhận ảnh: {res.get('description', resp.status_code)}")
    msg = res["result"]
    asyncio.create_task(_delete_message(api, msg["message_id"]))  # chạy nền, khỏi bắt người dùng chờ
    return msg["photo"][-1]["file_id"]


async def _delete_message(api: str, message_id: int):
    try:
        await net.request("POST", f"{api}/deleteMessage", data={"chat_id": ADMIN_CHAT, "message_id": message_id})
    except Exception as e:  # tin ảnh còn lại trong chat cũng không sao
        log.warning("Không xóa được tin ảnh QR: %s", e)


async def read_image(request: Request) -> tuple[bytes, str]:
    """Body = 1 file ảnh (≤ QR_MAX_BYTES). Trả về (bytes, content-type)."""
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("image/"):
        raise ValueError("Chỉ nhận file ảnh")
    # kiểm tra kích thước trước / trong lúc đọc, không đọc hết file lớn vào RAM rồi mới từ chối
    if int(request.headers.get("content-length") or 0) > QR_MAX_BYTES:
        raise ValueError("Ảnh lớn quá 10MB")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > QR_MAX_BYTES:
            raise ValueError("Ảnh lớn quá 10MB")
    if not data:
        raise ValueError("Ảnh trống")
    return bytes(data), ctype


async def store_upload(data: bytes, ctype: str) -> str:
    """Ảnh tải lên → file_id Telegram (bot dùng được) + lưu ảnh vào database."""
    file_id = await upload_qr(data, ctype)
    if ctype in ("image/jpeg", "image/jpg"):
        db.save_image(file_id, data)  # lưu luôn ảnh gốc
    else:
        await image_bytes(file_id)  # png/webp…: lấy bản JPEG Telegram đã chuyển
    return file_id


@app.post("/api/qr/{kind}/{rec_id}")
async def set_qr(kind: str, rec_id: int, request: Request):
    """Tải ảnh QR lên (body = ảnh). Lưu cho người đặt → mọi đơn của người đó, bot dùng luôn."""
    table = QR_TABLES.get(kind)
    if not table or not db.get(table, rec_id):
        raise HTTPException(404, "Không tìm thấy")
    data, ctype = await read_image(request)
    file_id = await store_upload(data, ctype)
    if kind == "person":
        db.set_person_qr(rec_id, file_id)
    else:
        db.set_qr(rec_id, file_id)
    return {"qr_v": qr_version(file_id)}


@app.delete("/api/qr/{kind}/{rec_id}")
def delete_qr(kind: str, rec_id: int):
    table = QR_TABLES.get(kind)
    if not table or not db.get(table, rec_id):
        raise HTTPException(404, "Không tìm thấy")
    if kind == "person":
        db.set_person_qr(rec_id, None)
    else:
        db.set_qr(rec_id, None)
    return {"ok": True}


@app.get("/api/qr/{kind}/{rec_id}")
async def qr(kind: str, rec_id: int, download: int = 0):
    r = db.get(QR_TABLES.get(kind, "orders"), rec_id)
    if not r or not r["qr_file_id"]:
        raise HTTPException(404, "Chưa có ảnh QR")
    headers = {"Cache-Control": "private, max-age=604800"}
    if download:  # nút ⬇️ Tải ảnh: tên file theo người đặt
        name = r["ten"] if kind == "person" else r["ten_nguoi_dat"]
        headers["Content-Disposition"] = f"attachment; filename=\"QR.jpg\"; filename*=UTF-8''{quote(f'QR {name}.jpg')}"
    return Response(await image_bytes(r["qr_file_id"]), media_type="image/jpeg", headers=headers)


# ---------- săn sale ----------

@app.get("/api/sales")
def sales(all: int = 0, q: str | None = None):
    return [sale_out(r) for r in db.list_sales(active_only=not all, keyword=q)]


@app.post("/api/sales")
async def create_sale(request: Request):
    """{"text": "- tên giá công X sl N shop Y\\nlink…"} (dán như bot) hoặc từng trường."""
    body = await request.json()
    if clean(body.get("text")):
        # nhóm: dòng đầu "nhóm 1" của danh sách, không thì ô Nhóm sale trên form
        nhom, text = sale_text.split_group(body["text"])
        nhom = nhom or convert("sales", "nhom", body.get("nhom"))
        items, errors = sale_text.parse_block(text)
        items = [{**it, "nhom": nhom} for it in items] if nhom else items
        if not items:
            raise ValueError("Không đọc được món nào: " + "; ".join(errors[:3]))
        ids = db.insert_many("sales", items, WEB_USER_ID)
        return {"ids": ids, "errors": errors}
    data = {"ten_sp": convert("sales", "ten", body.get("ten_sp")), "shop": clean(body.get("shop")),
            "so_luong": convert("sales", "so_luong", body.get("so_luong")), "link": clean(body.get("link")),
            "muc_gia": convert("sales", "muc_gia", body.get("muc_gia")),
            "uu_tien": convert("sales", "uu_tien", body.get("uu_tien")),
            "nhom": convert("sales", "nhom", body.get("nhom")),
            "gia_goc": convert("sales", "gia_goc", body.get("gia_goc")) or None,
            "giam_pt": convert("sales", "giam_pt", body.get("giam_pt")),
            "giam_toi_da": convert("sales", "giam_toi_da", body.get("giam_toi_da")) or None}
    if data["giam_pt"] and not data["gia_goc"]:
        raise ValueError("Nhập giá gốc để tính giá theo % giảm")
    data["gia_sale"], data["gia_sale_max"] = convert("sales", "gia_sale", body.get("gia_sale"))
    data["cong"], data["cong_max"] = convert("sales", "cong", body.get("cong"))
    return {"ids": [db.insert("sales", data, WEB_USER_ID)], "errors": []}


@app.post("/api/sales/{rec_id}/links")
async def add_links(rec_id: int, request: Request):
    body = await request.json()
    r = db.get("sales", rec_id)
    if not r:
        raise HTTPException(404, "Không tìm thấy món sale")
    new = [u for u in sale_text.URL_RE.findall(body.get("text", "")) if u not in (r["link"] or "").split()]
    if not new:
        raise ValueError("Không có link mới")
    db.update("sales", rec_id, "link", "\n".join([*(r["link"] or "").split(), *new]))
    return sale_out(db.get("sales", rec_id))


def _stock_result(raw: dict) -> dict:
    """Kết quả tiện ích Chrome gửi về → chỉ giữ các trường cần, cắt độ dài."""
    text = lambda v, n=200: str(v)[:n] if v not in (None, "") else None

    def num(v):
        try:
            return None if isinstance(v, bool) or v in (None, "") else int(float(v))
        except (TypeError, ValueError):
            return None

    skus = [{"name": text(s.get("name"), 100) or "Mặc định", "stock": num(s.get("stock")) or 0, "status": num(s.get("status"))}
            for s in (raw.get("skus") or [])[:50] if isinstance(s, dict)]
    return {"ok": bool(raw.get("ok")), "captcha": bool(raw.get("captcha")), "error": text(raw.get("error")),
            "title": text(raw.get("title")), "sold": num(raw.get("sold")), "total": num(raw.get("total")),
            "in_stock": bool(raw.get("in_stock")), "skus": skus}


@app.post("/api/sales/{rec_id}/stock")
async def sale_stock(rec_id: int, request: Request):
    """Tiện ích Chrome (static/extension) đọc tồn kho trên TikTok → trang web gửi về đây lưu theo link."""
    body = await request.json()
    r = db.get("sales", rec_id)
    if not r:
        raise HTTPException(404, "Không tìm thấy món sale")
    link = str(body.get("link", ""))
    if link not in (r["link"] or "").split():
        raise HTTPException(400, "Link không thuộc món sale này")
    db.set_sale_stock(rec_id, link, _stock_result(body.get("result") or {}))
    return sale_out(db.get("sales", rec_id))


@app.post("/api/sales/{rec_id}/variants")
async def sale_variants(rec_id: int, request: Request):
    """🎯 chọn phân loại cần mua của 1 link: {link, names: [...]} ([] = bỏ chọn)."""
    body = await request.json()
    r = db.get("sales", rec_id)
    if not r:
        raise HTTPException(404, "Không tìm thấy món sale")
    link = str(body.get("link", ""))
    if link not in (r["link"] or "").split():
        raise HTTPException(400, "Link không thuộc món sale này")
    names = [str(n) for n in body.get("names") or [] if str(n).strip()]
    db.set_sale_variants(rec_id, link, names)
    return sale_out(db.get("sales", rec_id))


@app.get("/api/tiktok-extension.zip")
def tiktok_extension(request: Request):
    """Tiện ích Chrome kiểm tra còn hàng, manifest gắn đúng địa chỉ web đang mở (content script chỉ chạy trên web này)."""
    match = f"{request.url.scheme}://{request.url.netloc}/*"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((STATIC / "extension").iterdir()):
            data = f.read_text(encoding="utf-8")
            # file nằm ngay gốc zip: Windows "Extract All" đã tự tạo thư mục quan-li-don-tiktok, có thư mục con nữa
            # thì thành 2 lớp lồng nhau → chọn thư mục ngoài, Chrome báo thiếu manifest
            z.writestr(f.name, data.replace("__WEB_MATCH__", match) if f.name == "manifest.json" else data)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="quan-li-don-tiktok.zip"'})


@app.post("/api/sales/all-active")
async def all_active(request: Request):
    body = await request.json()
    return {"changed": db.set_all_sales_active(1 if body.get("active") else 0)}


# ---------- danh mục ----------

@app.get("/api/products")
def products(q: str | None = None):
    """Sản phẩm kèm các dòng săn sale cùng tên (cả đang tắt); sale chưa có trong danh mục để riêng (orphans)."""
    sales: dict[str, list] = {}
    for r in db.list_sales(active_only=False):
        sales.setdefault(db.name_key(r["ten_sp"]), []).append(sale_out(r))
    images = db.product_images_map()
    imgs = lambda key: [r["id"] for r in images.get(key, [])]  # noqa: E731
    out = []
    for r in db.list_products(q):
        d = product_out(r)
        d["sales"] = sales.pop(r["ten_key"], [])
        d["images"] = imgs(r["ten_key"])
        out.append(d)
    orphans = [] if q else [{"ten_sp": ss[0]["ten_sp"], "sales": ss, "images": imgs(k)} for k, ss in sales.items()]
    return {"products": out, "orphans": orphans}


@app.post("/api/products")
async def create_product(request: Request):
    body = await request.json()
    name = convert("products", "ten", body.get("ten"))
    lo, hi = convert("products", "gia_goi_y", body.get("gia_goi_y"))
    if not db.add_product(name, lo, hi):
        raise ValueError(f"Đã có sản phẩm “{name}”")
    return {"ok": True}


@app.get("/api/people/defaults")
def person_defaults(name: str = ""):
    """Thêm đơn: chọn người đặt đã có → nguồn khách, SĐT, địa chỉ theo đơn gần nhất (giống bot)."""
    return db.person_defaults(name) or {}


@app.post("/api/people/{person_id}/merge")
async def merge_person(person_id: int, request: Request):
    """Gộp người này vào người khác: đơn chuyển sang tên người kia, người này bị xóa."""
    into = int((await request.json()).get("into") or 0)
    if into == person_id:
        raise ValueError("Chọn người khác để gộp vào")
    return {"moved": db.merge_people([person_id], into)}


@app.post("/api/people")
async def create_person(request: Request):
    body = await request.json()
    data = {f: convert("nguoi_dat", f, body.get(f)) for f, _ in db.PERSON_FIELDS}
    person_id = db.add_person(data)
    if not person_id:
        raise ValueError(f"Đã có người đặt “{data['ten']}”")
    return {"id": person_id}


@app.get("/api/people")
def people():
    stats = db.person_stats()
    texts = db.person_search_texts()
    out = []
    for p in db.list_people():
        d = dict(p)
        d["tim"] = texts.get(p["ten_key"], "")  # SĐT / tên trên đơn / địa chỉ trong đơn của họ (ô tìm kiếm)
        d.update(stats.get(p["ten_key"], {"ok": 0, "wait": 0, "fail": 0, "cong_da_nhan": 0, "theo_san": {}}))
        d["qr_v"] = qr_version(d["qr_file_id"])
        d["has_qr"] = bool(d.pop("qr_file_id"))
        out.append(d)
    return out


@app.get("/api/phones")
def phones():
    return db.list_phones()


@app.post("/api/phones")
async def create_phone(request: Request):
    body = await request.json()
    if not db.add_phone(str(body.get("sdt", "")), clean(body.get("ghi_chu")), mine=bool(body.get("cua_toi")),
                        device=clean(body.get("may"))):
        raise ValueError("Số không hợp lệ hoặc đã có")
    return {"ok": True}


@app.get("/api/last-mile")
def last_mile():
    """Tab 🏍 Giao hàng: bưu cục / kho giao cuối + nhân viên giao theo hãng, tổng hợp từ hành trình đã lưu."""
    return tracking.last_mile_summary()


# sổ địa chỉ đặt hàng (bot /diachi); sửa / xóa qua PATCH / DELETE /api/addresses/<id> chung
@app.get("/api/addresses")
def addresses(q: str = ""):
    return [dict(r) for r in db.list_addresses(q)]


@app.post("/api/addresses")
async def create_address(request: Request):
    return dict(db.get("addresses", db.add_address(str((await request.json()).get("noi_dung") or ""))))


@app.get("/api/phones/{phone}/orders")
def phone_orders(phone: str):
    return [order_out(r) for r in db.orders_by_phone(phone)]


@app.get("/api/money")
def money_preview(v: str, qty: int = 1, pct: str | None = None):
    """Xem trước cách đọc số tiền khi gõ (vd 4tr3 → 4.300.000đ). pct: v là giá gốc → giá sau giảm pct%."""
    if pct is not None:
        goc, p = parse_money(v), parse_pct(pct if "%" in pct else pct + "%")
        if not goc or not p:
            return {"text": ""}
        return {"text": f"{money(goc)} giảm {pct_text(p)} = {money(price_after_discount(goc, p))}"}
    got = parse_range(v)
    if not got:
        return {"text": ""}
    lo, hi = got
    text = money(lo) if not hi else range_short(lo, hi)
    if qty > 1 and lo and not hi:
        text += f" × {qty} = {money(lo * qty)}"
    return {"text": text, "lo": lo, "hi": hi}
