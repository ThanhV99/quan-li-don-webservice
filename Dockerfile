# Project web: FastAPI + database Postgres (cả ảnh QR / sản phẩm) + toàn bộ logic (tra vận đơn, sale, tiền) + API cho bot.
# Build: cd web && docker compose build   (context = thư mục web/)
# cố định bản Python + digest (build lại ra đúng image, không tự đổi bản vá); nâng cấp: đổi cả tag lẫn digest
FROM python:3.12.15-slim-trixie@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FILES_DIR=/app/data \
    TZ=Asia/Ho_Chi_Minh

WORKDIR /app

COPY Pipfile Pipfile.lock ./
RUN pip install pipenv \
    && pipenv install --system --deploy \
    && pip uninstall -y pipenv virtualenv

COPY *.py ./
# chứng chỉ trung gian bổ sung cho trang tra vận đơn (net.py)
COPY certs ./certs
COPY static ./static

# /app/data: bản chép ảnh QR theo tên người đặt (images/qr/) – ảnh gốc nằm trong database
VOLUME ["/app/data"]

USER 1000:1000

# Render (và nơi khác) cấp cổng qua biến PORT; chạy ở máy / docker compose: 8000.
# Sau proxy của Render đặt FORWARDED_ALLOW_IPS=* để uvicorn tin X-Forwarded-For (IP thật cho giới hạn đăng nhập sai).
CMD ["sh", "-c", "exec uvicorn web:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
