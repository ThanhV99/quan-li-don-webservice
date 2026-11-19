"""Quản lí tài khoản web từ dòng lệnh (chạy trong container: docker compose exec web python manage.py …).

  python manage.py createadmin <tên> [mật khẩu]   tạo tài khoản admin (không ghi mật khẩu → hỏi, gõ không hiện)
  python manage.py createuser <tên> [mật khẩu]    tạo tài khoản thường
  python manage.py password <tên> [mật khẩu]      đặt lại mật khẩu (đăng xuất mọi phiên của tài khoản đó)
  python manage.py setrole <tên> admin|user       đổi vai trò
  python manage.py lock <tên> / unlock <tên>      khóa / mở khóa
  python manage.py users                          danh sách tài khoản

  python manage.py import-sqlite <file.db> [--replace]  chép 1 file SQLite cũ (bản backup trước khi chuyển sang
                                                  Postgres) vào database Postgres; --replace = xóa dữ liệu đang có

Sao lưu / khôi phục database Postgres (chạy trong thư mục web/):
  docker compose exec -T postgres pg_dump -U quanlidon -Fc quanlidon > backup-$(date +%F).dump
  docker compose exec -T postgres pg_restore -U quanlidon -d quanlidon --clean --if-exists < backup-YYYY-MM-DD.dump
"""
import getpass
import os
import sqlite3
import sys

import db


def ask_password(args: list[str]) -> str:
    if len(args) > 1:
        return args[1]
    pw = getpass.getpass("Mật khẩu: ")
    if pw != getpass.getpass("Nhập lại: "):
        sys.exit("Hai lần nhập không khớp.")
    return pw


def need_user(name: str):
    u = db.find_web_user(name)
    if not u:
        sys.exit(f"Không có tài khoản “{name}”.")
    return u


SEEDED_TABLES = {"carriers", "products", "meta"}  # init_db điền sẵn khi database còn trống
SESSION_TABLE = "web_sessions"  # khóa ngoại tới web_users: chép sau, xóa trước


def ordered(tables: list[str]) -> list[str]:
    return sorted(tables, key=lambda t: t == SESSION_TABLE)


def open_readonly(path: str) -> sqlite3.Connection:
    """Mở file nguồn chỉ đọc; file WAL ở chỗ không ghi được (-shm) → mở dạng bất biến (file phải là bản chụp, không
    ai đang ghi: dừng web trước khi chép database thật)."""
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        return sqlite3.connect(uri + "&immutable=1", uri=True)


def target_name() -> str:
    return "Postgres (" + db.DATABASE_URL.split("@")[-1].split("/")[0].split(":")[0] + ")"


def pg_types(conn, table: str) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?", (table,))}


def coerce(value, pg_type: str | None):
    """Dữ liệu SQLite (kiểu lỏng: '' trong cột số …) → hợp kiểu cột Postgres."""
    if value is None or pg_type is None:
        return value
    if pg_type in ("bigint", "integer", "smallint"):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            v = value.strip()
            if not v:
                return None
            return int(float(v)) if v.replace(".", "", 1).lstrip("-").isdigit() else value
    if pg_type in ("double precision", "real", "numeric") and isinstance(value, str):
        v = value.strip()
        return float(v) if v else None
    if pg_type == "text" and not isinstance(value, str):
        return str(value)
    return value


def same_rows(got: list[tuple], want: list[tuple]) -> bool:
    """Giống nhau; số thực cho lệch ở chữ số rất nhỏ."""
    def eq(a, b):
        if isinstance(a, float) or isinstance(b, float):
            return a is not None and b is not None and abs(float(a) - float(b)) <= 1e-12 * max(abs(float(a)), 1.0)
        return a == b
    return len(got) == len(want) and all(len(g) == len(w) and all(map(eq, g, w)) for g, w in zip(got, want))


def import_sqlite(path: str, replace: bool):
    if not os.path.isfile(path):
        sys.exit(f"Không thấy file {path}")
    src = open_readonly(path)
    src_tables = ordered([r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")])
    with db.connect() as conn:
        have = set(db.list_tables(conn))
        missing = [t for t in src_tables if t not in have]
        if missing:
            sys.exit(f"Database đích thiếu bảng {missing} (init_db chưa tạo) – dừng.")
        # init_db tự thêm dữ liệu mặc định (hãng vận chuyển, sản phẩm mẫu, meta) → chỉ tính bảng có dữ liệu thật
        filled = [t for t in src_tables if t not in SEEDED_TABLES and conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()]
    if filled and not replace:
        sys.exit(f"Database đích ({target_name()}) đã có dữ liệu ở {filled}. Chạy lại với --replace để xóa và chép đè.")
    report = []
    with db.connect() as conn:  # 1 transaction: lỗi giữa chừng thì không ghi gì
        for t in reversed(src_tables):
            conn.execute(f"DELETE FROM {t}")
        for t in src_tables:
            dst_cols = db.table_columns(conn, t)
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})") if r[1] in dst_cols]
            types = pg_types(conn, t)
            rows = [tuple(coerce(v, types.get(c)) for v, c in zip(r, cols))
                    for r in src.execute(f"SELECT {', '.join(cols)} FROM {t}")]
            if rows:
                conn.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
            if "id" in cols:  # chép id có sẵn → chỉnh bộ đếm id tự tăng cho đơn mới không trùng
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) "
                             f"FROM {t}")
            report.append((t, cols, rows))
    bad = []
    with db.connect() as conn:  # đọc lại so từng bảng
        for t, cols, rows in report:
            got = sorted((tuple(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {t}")), key=repr)
            want = sorted(rows, key=repr)
            ok = same_rows(got, want)
            bad += [] if ok else [t]
            print(f"  {t:<18} {len(rows):>5} dòng {'✅' if ok else '❌ KHÁC'}")
    if bad:
        sys.exit(f"⚠️ Chép xong nhưng đọc lại khác ở {bad}")
    print(f"✅ Đã chép {path} → {target_name()}")


def main(argv: list[str]):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    db.init_db()
    if cmd == "import-sqlite" and args:
        import_sqlite(args[0], "--replace" in args[1:])
        return
    if cmd == "users":
        for u in db.list_web_users():
            state = "" if u["active"] else "  🔒 khóa"
            print(f"#{u['id']:<3} {u['username']:<20} {u['role']:<6} đăng nhập: {u['last_login'] or '-'}{state}")
        return
    if not args:
        sys.exit("Thiếu tên tài khoản. Xem: python manage.py help")
    if cmd in ("createadmin", "createuser"):
        role = "admin" if cmd == "createadmin" else "user"
        db.create_web_user(args[0], ask_password(args), role)
        print(f"✅ Đã tạo {role} “{args[0]}”.")
    elif cmd == "password":
        db.update_web_user(need_user(args[0])["id"], password=ask_password(args))
        print(f"✅ Đã đổi mật khẩu “{args[0]}”.")
    elif cmd == "setrole" and len(args) == 2:
        db.update_web_user(need_user(args[0])["id"], role=args[1])
        print(f"✅ “{args[0]}” → {args[1]}.")
    elif cmd in ("lock", "unlock"):
        db.update_web_user(need_user(args[0])["id"], active=cmd == "unlock")
        print(f"✅ Đã {'mở khóa' if cmd == 'unlock' else 'khóa'} “{args[0]}”.")
    else:
        sys.exit(f"Lệnh không hợp lệ: {cmd}. Xem: python manage.py help")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, RuntimeError) as e:
        sys.exit(f"⚠️ {e}")
