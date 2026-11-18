"""Quản lí tài khoản web từ dòng lệnh (chạy trong container: docker compose exec web python manage.py …).

  python manage.py createadmin <tên> [mật khẩu]   tạo tài khoản admin (không ghi mật khẩu → hỏi, gõ không hiện)
  python manage.py createuser <tên> [mật khẩu]    tạo tài khoản thường
  python manage.py password <tên> [mật khẩu]      đặt lại mật khẩu (đăng xuất mọi phiên của tài khoản đó)
  python manage.py setrole <tên> admin|user       đổi vai trò
  python manage.py lock <tên> / unlock <tên>      khóa / mở khóa
  python manage.py users                          danh sách tài khoản
"""
import getpass
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


def main(argv: list[str]):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    db.init_db()
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
    except ValueError as e:
        sys.exit(f"⚠️ {e}")
