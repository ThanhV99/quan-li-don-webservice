# Project web: FastAPI + database (giữ data/orders.db) + toàn bộ logic (tra vận đơn, sale, tiền) + API cho bot.
# Build: cd web && docker compose build   (context = thư mục web/)
# cố định bản Python + digest (build lại ra đúng image, không tự đổi bản vá); nâng cấp: đổi cả tag lẫn digest
FROM python:3.12.15-slim-trixie@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/app/data/orders.db \
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

# Database nằm trong volume /app/data để không mất khi build lại image
VOLUME ["/app/data"]

USER 1000:1000

CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
