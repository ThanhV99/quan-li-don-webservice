# Quản lí đơn – web service

Giao diện web + API cho hệ thống quản lý đơn đặt hộ Shopee / TikTok: giữ database (SQLite) và **toàn bộ logic**
(đơn hàng, trả công, thống kê, săn sale, tra hành trình SPX / J&T / GHN). Bot Telegram là project riêng, chỉ gọi API của web.

Python 3.12 · FastAPI · SQLite · Docker.

## Cài đặt

```bash
cp env.example .env              # điền SERVICE_TOKEN, BOT_TOKEN, ALLOWED_USER_IDS…
docker network create quan-li-don   # 1 lần: mạng chung với bot
docker compose up -d --build
docker compose exec web python manage.py createadmin <tên>   # tài khoản web đầu tiên
```

Web chạy ở `http://<máy chủ>:8088` (`WEB_PORT`). Dữ liệu nằm trong `./data/` (`DATA_DIR`): `orders.db`, ảnh QR, ảnh tải sẵn –
không đưa lên git; sao lưu bằng SQLite backup, không chép thẳng file khi web đang chạy.

Chạy trực tiếp (phát triển): `pipenv install && DB_PATH=./orders.db pipenv run uvicorn web:app --port 8088`.

## API

- `/api/*` cho giao diện: cần đăng nhập (cookie `qld_session`); `DELETE`, gộp người đặt, quản lý tài khoản chỉ admin.
  Mở không cần đăng nhập: `/api/login`, `/api/setup`, `/api/health`.
- `/api/svc/<module>.<hàm>` cho bot: header `X-Service-Token: <SERVICE_TOKEN>` (giống hệt bên bot), dữ liệu mã hóa theo `rpc.py`.

## File

| File | Vai trò |
|---|---|
| `web.py`, `static/` | API FastAPI và giao diện web (một trang) |
| `db.py` | truy cập SQLite, schema và migration |
| `tracking.py`, `alert_rules.py` | tra hành trình vận đơn, từ khóa / chu kỳ kiểm tra |
| `sale_text.py`, `utils.py` | săn sale, đọc / định dạng tiền, gom địa chỉ |
| `net.py`, `certs/` | kết nối thẳng ↔ proxy (`PROXY_URL`), chứng chỉ bổ sung |
| `rpc.py` | mã hóa dữ liệu cho API bot (bot giữ bản giống hệt) |
| `manage.py` | quản lý tài khoản web |
