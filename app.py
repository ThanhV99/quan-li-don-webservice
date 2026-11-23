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

from web import app  # noqa: E402,F401
