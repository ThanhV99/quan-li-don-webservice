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
trên chính máy này ở `localhost:5432`. Ảnh QR / ảnh sản phẩm lưu **trong database** (bảng `image_files`, theo `file_id`
Telegram), nên sao lưu database là có cả ảnh; `./data/` (`DATA_DIR`) chỉ giữ bản chép QR theo tên người đặt
(`images/qr/<tên>.jpg`). Cả hai không đưa lên git. Web tự bật Row Level Security cho mọi bảng (chặn Data API của
Supabase; web là chủ bảng nên không bị ảnh hưởng).

### Sao lưu / khôi phục

```bash
docker compose exec -T postgres pg_dump -U quanlidon -Fc quanlidon > backup-$(date +%F).dump
docker compose exec -T postgres pg_restore -U quanlidon -d quanlidon --clean --if-exists < backup-YYYY-MM-DD.dump
docker compose exec web python manage.py import-sqlite /app/data/<file>.db [--replace]   # nạp 1 bản backup SQLite cũ
```

### Chạy trên Render (Postgres ở nơi khác)

Web Service · Runtime Docker · repo này · Region Singapore · Health check `/api/health?bot=0`. Environment: các biến trong
`env.example` (không cần `PROXY_URL`, `PG_*`) + `DATABASE_URL` (Supabase / Neon / Render Postgres), `FORWARDED_ALLOW_IPS=*`,
`FILES_DIR=/tmp/quanlidon` (đĩa không giữ lâu; ảnh đã nằm trong database).
Cổng lấy từ `PORT` của Render.

### Chạy trên Vercel (Postgres trên Supabase)

Import repo vào Vercel (preset FastAPI, không cần build command): điểm vào `app.py` (= `web:app`), thư viện theo
`requirements.txt` (tạo lại bằng `pipenv requirements > requirements.txt` khi đổi Pipfile), Python theo `.python-version`.
Environment: `DATABASE_URL` = chuỗi **Transaction pooler** của Supabase (cổng 6543),
`FORWARDED_ALLOW_IPS=*`, `BOT_TOKEN`, `ALLOWED_USER_IDS`, các `VC_*` / `ADDR_ALIASES`, `ALERT_BOT_TOKEN` / `ALERT_CHAT_IDS`
(cảnh báo vận chuyển, xem dưới) (không cần `PROXY_URL`, `PG_*`).
Vercel tự đặt `VERCEL=1` → web bỏ vòng lặp ảnh chạy nền và thư mục ảnh QR theo tên, `FILES_DIR` mặc định `/tmp/quan-li-don`.
Vercel không cho đặt `TZ`: `app.py` tự đặt giờ Việt Nam (đổi bằng `APP_TZ`).

Chạy trực tiếp (phát triển, cần 1 Postgres): `pipenv install && DATABASE_URL=postgresql://… pipenv run uvicorn web:app --port 8088`.

## Kiểm tra vận chuyển và cảnh báo (`shipwatch.py`)

Web tự tra hành trình các đơn **Đã đặt** có mã vận đơn tra được (SPX, J&T; GHN đang bắt captcha nên dùng hành trình
tiện ích Chrome gửi về; BEST chỉ có link web) và **chỉ gửi tin** Telegram qua Bot API, không cần bot nào chạy
liên tục. Đơn đã rời Đã đặt không tra theo lịch này nữa.

### Ai gọi, gọi khi nào

Mỗi lần web được gọi = **1 lượt** (`shipwatch.run_pass`): chỉ tra những đơn đã **tới giờ hẹn riêng** của đơn đó
(`orders.vc_next_check`), đơn chưa tới giờ thì bỏ qua (không gọi hãng). Phần lớn các lượt chỉ đọc database rồi xong.

| Web chạy ở | Cách đánh thức |
|---|---|
| Vercel | cron ngoài gọi `GET`/`POST /api/cron/shipments?budget=120` **30 phút/lần, chỉ 6:00–21:00 giờ Việt Nam** (`.github/workflows/shipwatch.yml`, lịch GitHub tính theo UTC; hoặc cron-job.org), header `Authorization: Bearer <SERVICE_TOKEN>` (hoặc `CRON_SECRET`) |
| Docker trên máy | `SHIPMENT_WATCH=1`: web tự chạy nền, ngủ tới giờ hẹn sớm nhất, **tối đa 10 phút** |

Khoảng cách 2 lần đánh thức = độ trễ tối đa so với giờ hẹn (vòng lặp cũ của bot: tối đa 10 phút). Trên Vercel đặt
30 phút: đơn sắp giao thực tế tra khoảng 1–1,5 giờ/lần, đơn mới có mã được tra lần đầu trong khoảng 30–60 phút.
Ngoài 6:00–21:00 không ai đánh thức → không tra, không gửi tin; đơn tới hạn trong đêm được tra dồn từ lượt 6:00
(mỗi lượt tối đa 120 giây ≈ 25–40 đơn, còn lại để lượt sau). Một lúc chỉ 1 lượt chạy (khóa trong bảng `meta`,
`shipwatch_lease`); `budget` = số giây tối đa của 1 lượt (Vercel), hết giờ thì đơn chưa tra giữ nguyên giờ hẹn → lượt sau tra tiếp.

### Giờ hẹn của mỗi đơn

Sau mỗi lần tra, đơn được hẹn lần sau theo **mức** của hành trình (`orders.vc_near`, `tracking.delivery_level`),
cộng một độ lệch ngẫu nhiên (để các đơn không bị tra dồn cùng lúc; độ lệch không quá nửa chu kỳ):

| Mức | Khi nào | Tra lại sau | Hằng số (`alert_rules.py`) |
|---|---|---|---|
| Đơn mới có mã | chưa có giờ hẹn | lần đầu trong **0–30 phút** | `FIRST_CHECK_MINUTES` |
| 0 · thường | chưa tới kho cuối | **12 giờ ± 60 phút** | `TRACK_INTERVAL_HOURS` (env), `CHECK_JITTER_MINUTES` |
| 2 · về kho cuối | tới kho trung chuyển cuối trước bưu cục giao (`VC_HUB_*`) | **6 giờ ± 30 phút** | `HUB_CHECK_HOURS`, `HUB_CHECK_JITTER_MINUTES` |
| 3 · kho cuối đã phân loại | kho cuối báo đã phân loại / đang chuyển tới bưu cục giao | **3 giờ ± 20 phút** | `HUB_SORTED_CHECK_HOURS`, `HUB_SORTED_CHECK_JITTER_MINUTES` |
| 1 · sắp giao | tới bưu cục giao (`VC_ALERT_PLACES`, `VC_JT_AREA`), có mốc "đang giao", hoặc ngày dự kiến giao ≤ hôm nay + `NEAR_ETA_DAYS` | **1 giờ ± 30 phút** | `NEAR_CHECK_HOURS`, `NEAR_CHECK_JITTER_MINUTES` |

- Sắp giao thắng về kho cuối. Tra hãng **lỗi** → dùng hành trình đã lưu để tính mức; vẫn hẹn lại theo mức hiện tại (không tra dồn liên tục).
- Hành trình đã **kết thúc** (đã giao / hoàn / hủy xong) được lưu trong `track_logs` và không bao giờ gọi hãng lại cho mã đó.
- Đơn đã rời Đã đặt mà hành trình lưu chưa kết thúc: tra lại khoảng **3 giờ/lần** (`UNFINISHED_LOG_HOURS`), chỉ cập nhật hành trình, không gửi tin.
- Đổi `alert_rules.py` hoặc các `VC_*`: lượt đầu sau deploy tính lại mức mọi đơn từ hành trình đã lưu (không gọi hãng); lên mức tra dày hơn thì giờ hẹn được kéo sớm lại trong 1 chu kỳ mới.
- **Giãn cách gọi hãng:** 2 giây giữa 2 đơn trong 1 lượt, và mỗi hãng cách nhau tối thiểu `API_MIN_GAP_SECONDS` (2 giây, dùng chung cho mọi nơi gọi, qua bảng `meta`).

### Khi nào gửi tin

- Mốc mới khớp từ khóa cảnh báo (`ALERT_KEYWORDS`: đang giao, giao lỗi, đã giao…) hoặc hủy / hoàn (`CANCEL_KEYWORDS`) → tin tới mọi người nhận. **Mỗi mốc chỉ báo 1 lần** (`orders.vc_alert_last`); gửi lỗi thì lượt sau thử lại.
- Đơn bị hủy / hoàn → thêm 1 tin cho admin kèm link mở đơn trên web để tự chuyển Đã hủy (trạng thái không bao giờ tự đổi).
- **Bảng "đơn đang giao đến"** gửi lại khi có đơn mới vào danh sách sắp giao, hoặc đơn chuyển từ 📮 sắp giao sang 🏍 đang giao (`meta.delivering_ids`).
- Gửi bằng `ALERT_BOT_TOKEN` (trống thì `BOT_TOKEN`) tới `ALERT_CHAT_IDS` (trống thì `ALLOWED_USER_IDS`) + người đã được duyệt trong `alert_users`; người nhận phải từng bấm Start với bot đó. Link trong tin mở `PUBLIC_URL/don-hang?don=<id>&tra=1` (Vercel tự lấy tên miền production).

## API

- `/api/*` cho giao diện: cần đăng nhập (cookie `qld_session`); `DELETE`, gộp người đặt, quản lý tài khoản chỉ admin.
  Mở không cần đăng nhập: `/api/login`, `/api/setup`, `/api/health`.
- `/api/cron/shipments`: 1 lượt kiểm tra vận chuyển, cần token (xem trên).
- `/api/svc/<module>.<hàm>` cho bot: header `X-Service-Token: <SERVICE_TOKEN>` (giống hệt bên bot), dữ liệu mã hóa theo `rpc.py`.
  Bot tải mã nguồn các module tính toán thuần (`utils`, `alert_rules`, `net`, `sale_text`, `tracking`, `track_text`) qua `<module>.__source__`
  để chạy tại chỗ; đọc / ghi dữ liệu và tra vận đơn vẫn gọi API.

## File

| File | Vai trò |
|---|---|
| `web.py`, `static/` | API FastAPI và giao diện web (một trang) |
| `db.py` | truy cập database Postgres, schema và migration |
| `tracking.py`, `alert_rules.py` | tra hành trình vận đơn, từ khóa / chu kỳ kiểm tra |
| `shipwatch.py`, `track_text.py` | tự kiểm tra vận chuyển theo lịch + gửi tin Telegram, chữ của tin |
| `sale_text.py`, `utils.py` | săn sale, đọc / định dạng tiền, gom địa chỉ |
| `net.py`, `certs/` | kết nối thẳng ↔ proxy (`PROXY_URL`), chứng chỉ bổ sung |
| `rpc.py` | mã hóa dữ liệu cho API bot (bot giữ bản giống hệt) |
| `manage.py` | quản lý tài khoản web, nạp bản backup SQLite cũ (`import-sqlite`) |
