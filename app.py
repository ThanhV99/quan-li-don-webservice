"""Điểm vào cho Vercel (tự tìm biến `app` trong app.py). Chạy local / Docker vẫn dùng `uvicorn web:app`."""
from web import app  # noqa: F401
