"""Kết nối mạng tự đổi đường: đi thẳng hoặc qua proxy (PROXY_URL trong .env).

Máy chạy bot lúc ở mạng công ty (bắt buộc qua proxy), lúc ở ngoài (proxy không tới được).
Mọi request (Telegram + tra vận đơn) thử đường đang dùng trước; nếu lỗi kết nối thì thử
đường còn lại và nhớ đường chạy được cho các request sau. Không đặt PROXY_URL = luôn đi thẳng.
Web dùng cho tra vận đơn và tải ảnh Telegram; kết nối Telegram của bot có bản riêng ở bot/tg_net.py.
"""
import glob
import logging
import os
import ssl
import time

import certifi
import httpx

log = logging.getLogger(__name__)

# lỗi do đường mạng (không tới được server / proxy) → đáng thử đường khác.
# Lỗi đọc (server chậm trả lời) thì không thử lại để tránh gửi trùng tin.
ROUTE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)

# Chứng chỉ trung gian (certs/*.pem) mà vài trang tra vận đơn quên gửi kèm, trình duyệt tự tải bù còn
# Python thì báo CERTIFICATE_VERIFY_FAILED. Vd: jtexpress.vn (từ 09/2026) thiếu "Sectigo DV R36".
CERTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    for path in sorted(glob.glob(os.path.join(CERTS_DIR, "*.pem"))):
        ctx.load_verify_locations(cafile=path)
    return ctx


SSL_CONTEXT = _ssl_context()

_use_proxy = False


def proxy_url() -> str | None:
    return os.getenv("PROXY_URL", "").strip() or None


def routes() -> list[str | None]:
    """Các đường theo thứ tự thử: đường đang dùng trước. None = đi thẳng."""
    proxy = proxy_url()
    if not proxy:
        return [None]
    return [proxy, None] if _use_proxy else [None, proxy]


def _remember(proxy: str | None):
    global _use_proxy
    if (proxy is not None) != _use_proxy:
        _use_proxy = proxy is not None
        log.info("Mạng: chuyển sang %s", f"proxy {proxy}" if proxy else "kết nối trực tiếp")


def route_name() -> str:
    return f"proxy {proxy_url()}" if _use_proxy else "kết nối trực tiếp"


async def request(method: str, url: str, *, timeout: float = 15, headers=None,
                  follow_redirects: bool = False, **kwargs) -> httpx.Response:
    """httpx request có đổi đường. Lỗi khác lỗi đường mạng được ném ra như httpx bình thường."""
    last = None
    for proxy in routes():
        try:
            # trust_env=False: không lấy HTTPS_PROXY của máy, đường đi do module này quyết định
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=6), headers=headers,
                                         proxy=proxy, follow_redirects=follow_redirects,
                                         trust_env=False, verify=SSL_CONTEXT) as client:
                resp = await client.request(method, url, **kwargs)
        except ROUTE_ERRORS as e:
            last = e
            continue
        _remember(proxy)
        return resp
    raise last
