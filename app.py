"""Điểm vào cho Vercel (tự tìm biến `app` trong app.py). Chạy local / Docker vẫn dùng `uvicorn web:app`."""
import os
import time

# Vercel giữ biến TZ (hàm luôn chạy giờ UTC) → đặt múi giờ ở đây, TRƯỚC khi import web (mọi mốc giờ lấy từ datetime.now()).
# Đổi bằng biến APP_TZ nếu cần. Máy không có dữ liệu múi giờ → dùng chuỗi POSIX "ICT-7" (UTC+7, Việt Nam không đổi giờ).
os.environ["TZ"] = os.getenv("APP_TZ") or "Asia/Ho_Chi_Minh"
time.tzset()
if time.timezone == 0 and os.environ["TZ"] == "Asia/Ho_Chi_Minh":
    os.environ["TZ"] = "ICT-7"
    time.tzset()

try:
    from web import app  # noqa: E402,F401
except Exception as _e:  # noqa: BLE001 – web không khởi động được: trả lỗi ra trang thay vì 500 trống của Vercel
    import logging
    import re
    import traceback

    logging.exception("Web không khởi động được")
    _tb = traceback.extract_tb(_e.__traceback__)
    _where = " ← ".join(f"{os.path.basename(f.filename)}:{f.lineno}" for f in reversed(_tb[-4:]))
    # xóa mật khẩu trong chuỗi kết nối (postgresql://user:mật-khẩu@host) nếu lời báo lỗi có in ra
    _msg = re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1***@", f"{type(_e).__name__}: {_e}")[:1500]

    async def app(scope, receive, send):  # ASGI tối thiểu, không cần FastAPI
        if scope["type"] != "http":
            return
        body = f"Web không khởi động được\n{_msg}\nTại: {_where}\n".encode()
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": body})
