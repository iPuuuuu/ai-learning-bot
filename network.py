from __future__ import annotations

import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _proxy_is_reachable(proxy: str) -> bool:
    parsed = urlparse(proxy)
    if not parsed.hostname or not parsed.port:
        return True
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.25):
            return True
    except OSError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def open_with_fallback(request, timeout: int, proxy_urls: str = "", allow_redirects: bool = True):
    """GET may try configured proxies; paid POST goes to one endpoint exactly once."""
    proxies = [item.strip() for item in proxy_urls.split(",") if item.strip() and _proxy_is_reachable(item.strip())]
    method = request.get_method().upper()
    attempts = (proxies[:1] or [""]) if method not in {"GET", "HEAD"} else ([""] + proxies)
    last_error = None
    for proxy in attempts:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({})
        handlers = [handler]
        if not allow_redirects:
            handlers.append(_NoRedirect())
        try:
            attempt_timeout = min(timeout, 6) if not proxy and method in {"GET", "HEAD"} and proxies else timeout
            return urllib.request.build_opener(*handlers).open(request, timeout=attempt_timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("没有可用的网络连接方式")
