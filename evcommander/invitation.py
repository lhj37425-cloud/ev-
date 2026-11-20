"""Single-account invitation gate for the isolated EV Commander trial."""

import hashlib
import hmac
import json
from collections import deque
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from threading import Lock
from time import monotonic, time
from urllib.parse import parse_qs, urlsplit

import tenancy


COOKIE = "ev_commander_session"
SESSION_SECONDS = 8 * 3600


class Invitation:
    def __init__(self, origin, config_file):
        address = urlsplit(origin)
        if origin not in {"https://evcommander.evoldream.tech", "http://127.0.0.1:8766"}:
            raise ValueError("Invalid EV Commander trial origin")
        self.origin = origin
        self.host = address.netloc
        self.config = Path(config_file)
        self.failures = deque(maxlen=12)
        self.lock = Lock()

    def credentials(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        return (data["username"], bytes.fromhex(data["salt"]),
                bytes.fromhex(data["password_hash"]), bytes.fromhex(data["session_key"]))

    def session(self, request):
        cached = getattr(request, "_ev_session", None)
        if cached is not None:
            return cached or False
        identity = self._session(request)
        request._ev_session = identity
        return identity or False

    def token(self, request):
        """Return the signed session token from the cookie, without DB lookup."""
        try:
            cookie = SimpleCookie()
            cookie.load(request.headers.get("Cookie", ""))
            value = cookie[COOKIE].value
            expiry, token, signature = value.split(".")
            if not 0 < int(expiry) - int(time()) <= SESSION_SECONDS:
                return None
            expected = self.signature(self.credentials()[3], expiry + "." + token)
            return token if hmac.compare_digest(signature, expected) else None
        except (CookieError, KeyError, ValueError, OSError):
            return None

    def _session(self, request):
        token = self.token(request)
        return tenancy.session_identity(token) if token else None

    @staticmethod
    def signature(key, value):
        return hmac.new(key, value.encode("ascii"), hashlib.sha256).hexdigest()

    def set_cookie(self, request, token):
        expiry = int(time()) + SESSION_SECONDS
        value = f"{expiry}.{token}"
        signature = self.signature(self.credentials()[3], value)
        request.send_header(
            "Set-Cookie",
            f"{COOKIE}={value}.{signature}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Lax",
        )

    def authenticate(self, request):
        if request.headers.get("Origin") != self.origin or request.headers.get_content_type() != "application/x-www-form-urlencoded":
            return 403, None
        try:
            size = int(request.headers.get("Content-Length", "0"))
            if not 0 < size <= 2048:
                return 400, None
            fields = parse_qs(request.rfile.read(size).decode("utf-8"), max_num_fields=4)
            username = fields.get("username", [""])[0]
            password = fields.get("password", [""])[0]
            if not 1 <= len(password) <= 128 or len(username) > 64:
                return 400, None
            with self.lock:
                now = monotonic()
                while self.failures and self.failures[0] < now - 60:
                    self.failures.popleft()
                if len(self.failures) >= 12:
                    return 429, None
                self.failures.append(now)
            result = tenancy.authenticate(username, password)
            if result is not None:
                identity, token = result
                request._ev_session = identity
                return 303, token
            return 401, None
        except (ValueError, KeyError, OSError):
            return 400, None


def login_page(status):
    error = {401: "账号或口令不正确。", 400: "请检查账号和口令后重试。",
             403: "请重新打开登录页。", 429: "尝试次数过多，请稍候再试。"}.get(status, "")
    banner = f'<p class="error" role="alert">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>邀请登录 · EV指挥官</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#f7f4f1;color:#291d1d;font:16px system-ui,sans-serif}}
main{{background:white;border:1px solid #e6dfda;border-radius:16px;width:min(100%,400px);padding:32px}}
h1{{margin:0 0 8px}}p{{margin:0 0 24px;color:#665956}}
label{{display:block;margin:14px 0 6px}}
input,button{{width:100%;min-height:46px;border-radius:8px;font:inherit}}
input{{border:1px solid #b8aba6;padding:10px 12px;color:inherit}}
button{{margin-top:22px;border:0;background:#971c19;color:white;cursor:pointer}}
.error{{color:#a01c19;margin-bottom:14px}}
</style>
<main><h1>EV指挥官</h1><p>使用邀请账号登录</p>{banner}
<form action="/invite" method="post">
<label for="username">账号</label>
<input id="username" name="username" autocomplete="username" required maxlength="64" autocapitalize="none">
<label for="password">口令</label>
<input id="password" name="password" type="password" autocomplete="current-password" required maxlength="128">
<button>登录</button></form></main></html>""".encode("utf-8")
