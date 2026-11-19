# Quản lí đơn – web service

Giao diện web + API cho hệ thống quản lý đơn đặt hộ Shopee / TikTok: giữ database (Postgres) và **toàn bộ logic**
(đơn hàng, trả công, thống kê, săn sale, tra hành trình SPX / J&T / GHN). Bot Telegram là project riêng, chỉ gọi API của web.

Python 3.12 · FastAPI · PostgreSQL · Docker.

## Cài đặt

```bash
cp env.example .env              # điền SERVICE_TOKEN, BOT_TOKEN, ALLOWED_USER_IDS, PG_PASSWORD, DATABASE_URL…
docker network create quan-li-don   # 1 lần: mạng chung với bot
docker compose up -d --build        # chạy Postgres (quan-li-don-pg) + web
docker compose exec web python manage.py createadmin <tên>   # tài khoản web đầu tiên
```

Web chạy ở `http://<máy chủ>:8088` (`WEB_PORT`). Database Postgres nằm trong `./pgdata/` (`PG_DATA_DIR`), mở cho pgAdmin / psql
trên chính máy này ở `localhost:5432`. Ảnh QR / sản phẩm tải sẵn nằm trong `./data/` (`DATA_DIR`). Cả hai không đưa lên git.

### Sao lưu / khôi phục

```bash
docker compose exec -T postgres pg_dump -U quanlidon -Fc quanlidon > backup-$(date +%F).dump
docker compose exec -T postgres pg_restore -U quanlidon -d quanlidon --clean --if-exists < backup-YYYY-MM-DD.dump
docker compose exec web python manage.py import-sqlite /app/data/<file>.db [--replace]   # nạp 1 bản backup SQLite cũ
```

### Chạy trên Render (Postgres ở nơi khác)

Web Service · Runtime Docker · repo này · Region Singapore · Health check `/api/health?bot=0`. Environment: các biến trong
`env.example` (không cần `PROXY_URL`, `PG_*`) + `DATABASE_URL` (Supabase / Neon / Render Postgres), `FORWARDED_ALLOW_IPS=*`.
Cổng lấy từ `PORT` của Render.

Chạy trực tiếp (phát triển, cần 1 Postgres): `pipenv install && DATABASE_URL=postgresql://… pipenv run uvicorn web:app --port 8088`.

## API

- `/api/*` cho giao diện: cần đăng nhập (cookie `qld_session`); `DELETE`, gộp người đặt, quản lý tài khoản chỉ admin.
  Mở không cần đăng nhập: `/api/login`, `/api/setup`, `/api/health`.
- `/api/svc/<module>.<hàm>` cho bot: header `X-Service-Token: <SERVICE_TOKEN>` (giống hệt bên bot), dữ liệu mã hóa theo `rpc.py`.
  Bot tải mã nguồn các module tính toán thuần (`utils`, `alert_rules`, `net`, `sale_text`, `tracking`) qua `<module>.__source__`
  để chạy tại chỗ; đọc / ghi dữ liệu và tra vận đơn vẫn gọi API.

## File

| File | Vai trò |
|---|---|
| `web.py`, `static/` | API FastAPI và giao diện web (một trang) |
| `db.py` | truy cập database Postgres, schema và migration |
| `tracking.py`, `alert_rules.py` | tra hành trình vận đơn, từ khóa / chu kỳ kiểm tra |
| `sale_text.py`, `utils.py` | săn sale, đọc / định dạng tiền, gom địa chỉ |
| `net.py`, `certs/` | kết nối thẳng ↔ proxy (`PROXY_URL`), chứng chỉ bổ sung |
| `rpc.py` | mã hóa dữ liệu cho API bot (bot giữ bản giống hệt) |
| `manage.py` | quản lý tài khoản web, nạp bản backup SQLite cũ (`import-sqlite`) |
