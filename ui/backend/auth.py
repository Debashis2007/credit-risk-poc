"""Console login: per-deployment users, signed session cookie, login throttling.

Configuration (per deployment, never shared between projects):
  CONSOLE_USERS           comma-separated "username:<hash>" entries; hash from
                          `python ui/backend/auth.py hash`
  CONSOLE_SESSION_SECRET  random string that signs session cookies (`python ui/backend/auth.py secret`)
  CONSOLE_PROJECT         project label; part of the cookie name and bound into every session
  CONSOLE_SESSION_HOURS   session lifetime, default 12
  CONSOLE_AUTH_REQUIRED   "1" refuses to start without users and a secret (set in the hosted image)

Without CONSOLE_USERS (local development) the console is open. The cookie has no Domain attribute,
so browsers send it only to the host that set it, never to other projects' subdomains.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ITERATIONS = 310_000
MAX_FAILURES, FAILURE_WINDOW_S = 5, 15 * 60
OPEN_PATHS = {"/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/me"}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, digest = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(actual, _unb64(digest))
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, env: Optional[dict[str, str]] = None):
        env = dict(os.environ if env is None else env)
        self.users: dict[str, str] = {}
        for entry in filter(None, (e.strip() for e in env.get("CONSOLE_USERS", "").split(","))):
            name, _, encoded = entry.partition(":")
            if not name or not encoded:
                raise ValueError("CONSOLE_USERS entries must be username:hash")
            self.users[name.strip().lower()] = encoded.strip()
        self.secret = env.get("CONSOLE_SESSION_SECRET", "").encode()
        self.project = env.get("CONSOLE_PROJECT", "console").strip() or "console"
        self.cookie = re.sub(r"[^a-z0-9]+", "_", self.project.lower()).strip("_") + "_session"
        self.ttl_s = int(float(env.get("CONSOLE_SESSION_HOURS", "12")) * 3600)
        self.enabled = bool(self.users)
        if self.enabled and len(self.secret) < 32:
            raise ValueError("CONSOLE_SESSION_SECRET must be at least 32 characters when CONSOLE_USERS is set")
        if env.get("CONSOLE_AUTH_REQUIRED") == "1" and not self.enabled:
            raise ValueError("CONSOLE_AUTH_REQUIRED=1 but CONSOLE_USERS is not set")
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._dummy = hash_password(secrets.token_hex(8))

    # ----- sessions ----------------------------------------------------------------------
    def issue(self, user: str, now: Optional[float] = None) -> str:
        payload = _b64(json.dumps({"u": user, "p": self.project, "exp": int((time.time() if now is None else now) + self.ttl_s)},
                                  separators=(",", ":")).encode())
        return f"{payload}.{_b64(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest())}"

    def user_for(self, token: Optional[str], now: Optional[float] = None) -> Optional[str]:
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        expected = _b64(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if data.get("p") != self.project or data.get("exp", 0) < (time.time() if now is None else now):
            return None
        return data["u"] if data.get("u") in self.users else None

    # ----- login -------------------------------------------------------------------------
    def throttled(self, client: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
            self._failures[client] = recent
            return len(recent) >= MAX_FAILURES

    def check(self, client: str, username: str, password: str) -> Optional[str]:
        user = (username or "").strip().lower()
        encoded = self.users.get(user)
        ok = verify_password(password or "", encoded or self._dummy) and encoded is not None
        if ok:
            with self._lock:
                self._failures.pop(client, None)
            return user
        with self._lock:
            self._failures.setdefault(client, []).append(time.time())
        return None


class Login(BaseModel):
    username: str
    password: str


def install(app: Any, auth: Auth) -> None:
    def client_ip(request: Request) -> str:
        # The edge proxy appends the real peer; earlier entries are client-supplied.
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",")[-1].strip() or (request.client.host if request.client else "unknown")

    def secure(request: Request) -> bool:
        return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if auth.enabled and path.startswith("/api/") and path not in OPEN_PATHS:
            user = auth.user_for(request.cookies.get(auth.cookie))
            if not user:
                return JSONResponse({"detail": "login required"}, status_code=401)
            request.state.login = user
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/auth/me")
    def me(request: Request):
        if not auth.enabled:
            return {"enabled": False, "user": None, "project": auth.project}
        user = auth.user_for(request.cookies.get(auth.cookie))
        if not user:
            return JSONResponse({"enabled": True, "user": None, "project": auth.project}, status_code=401)
        return {"enabled": True, "user": user, "project": auth.project}

    @app.post("/api/auth/login")
    def login(body: Login, request: Request):
        if not auth.enabled:
            return {"enabled": False, "user": None, "project": auth.project}
        client = client_ip(request)
        if auth.throttled(client):
            return JSONResponse({"detail": "too many failed attempts; try again in 15 minutes"}, status_code=429)
        user = auth.check(client, body.username, body.password)
        if not user:
            return JSONResponse({"detail": "invalid username or password"}, status_code=401)
        response = JSONResponse({"enabled": True, "user": user, "project": auth.project})
        response.set_cookie(auth.cookie, auth.issue(user), max_age=auth.ttl_s, httponly=True,
                            secure=secure(request), samesite="strict", path="/")
        return response

    @app.post("/api/auth/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.cookie, path="/")
        return response


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "hash":
        password = getpass.getpass("Password: ")
        if len(password) < 12:
            raise SystemExit("Use at least 12 characters.")
        if password != getpass.getpass("Repeat: "):
            raise SystemExit("Passwords do not match.")
        print(hash_password(password))
    elif command == "secret":
        print(secrets.token_urlsafe(48))
    else:
        raise SystemExit("usage: python ui/backend/auth.py hash|secret")


if __name__ == "__main__":
    main()
