"""账号、密码、会话与 CSRF 防护。"""

from __future__ import annotations

import functools
import hmac
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Flask, Request, current_app, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import execute, query_all, query_one, row_to_dict, utcnow

PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
PASSWORD_SYMBOLS = "!@#%^*-_=+"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")


class AuthError(Exception):
    """认证/授权失败，带 HTTP 语义。"""

    def __init__(self, message: str, code: int = 400, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retry_after = retry_after


@dataclass
class Principal:
    id: int
    username: str
    role: str
    must_change: bool
    disabled: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "must_change": self.must_change,
            "is_admin": self.is_admin,
        }


# --------------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        return False


def generate_password(length: int = 16, symbols: bool = True) -> str:
    """生成随机密码：去掉 0/O/1/l/I 之类易混字符。"""
    alphabet = PASSWORD_ALPHABET
    if symbols:
        alphabet += PASSWORD_SYMBOLS
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.isdigit() for c in candidate)
            and any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
        ):
            return candidate


def validate_new_password(password: str, minimum: int) -> None:
    if len(password or "") < max(6, int(minimum)):
        raise AuthError(f"密码至少 {max(6, int(minimum))} 位")
    if password.strip() != password:
        raise AuthError("密码首尾不能有空格")
    if len(set(password)) < 4:
        raise AuthError("密码过于简单，请换一个")


def validate_username(username: str) -> None:
    if not USERNAME_RE.match(username or ""):
        raise AuthError("用户名只能是 3-32 位字母、数字、下划线、点或短横线")


# --------------------------------------------------------------------- 用户
def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str = "user",
    must_change: bool = False,
) -> dict[str, Any]:
    validate_username(username)
    if role not in {"admin", "user"}:
        raise AuthError("角色只能是 admin 或 user")
    exists = query_one(conn, "SELECT id FROM users WHERE username = ?", (username,))
    if exists:
        raise AuthError(f"用户 {username} 已存在", 409)
    execute(
        conn,
        "INSERT INTO users(username, password_hash, role, must_change, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (username, hash_password(password), role, int(must_change), utcnow()),
    )
    row = query_one(conn, "SELECT * FROM users WHERE username = ?", (username,))
    return row_to_dict(row)


def get_user(conn: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    return row_to_dict(query_one(conn, "SELECT * FROM users WHERE username = ?", (username,)))


def get_user_by_id(conn: sqlite3.Connection, uid: int) -> dict[str, Any] | None:
    return row_to_dict(query_one(conn, "SELECT * FROM users WHERE id = ?", (uid,)))


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = query_all(
        conn,
        "SELECT id, username, role, disabled, must_change, created_at, last_login_at "
        "FROM users ORDER BY id",
    )
    return [row_to_dict(row) for row in rows]


def count_users(conn: sqlite3.Connection) -> int:
    row = query_one(conn, "SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


def set_password(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    must_change: bool = False,
    minimum: int = 8,
) -> None:
    validate_new_password(password, minimum)
    user = get_user(conn, username)
    if not user:
        raise AuthError(f"用户 {username} 不存在", 404)
    execute(
        conn,
        "UPDATE users SET password_hash = ?, must_change = ? WHERE id = ?",
        (hash_password(password), int(must_change), user["id"]),
    )


def delete_user(conn: sqlite3.Connection, username: str) -> None:
    user = get_user(conn, username)
    if not user:
        raise AuthError(f"用户 {username} 不存在", 404)
    admins = query_one(conn, "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0")
    if user["role"] == "admin" and admins and int(admins["n"]) <= 1:
        raise AuthError("不能删除最后一个管理员账号")
    execute(conn, "DELETE FROM users WHERE id = ?", (user["id"],))


def set_disabled(conn: sqlite3.Connection, username: str, disabled: bool) -> None:
    user = get_user(conn, username)
    if not user:
        raise AuthError(f"用户 {username} 不存在", 404)
    if disabled and user["role"] == "admin":
        admins = query_one(conn, "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0")
        if admins and int(admins["n"]) <= 1:
            raise AuthError("不能停用唯一的管理员账号")
    execute(conn, "UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user["id"]))


def bootstrap_admin(config, conn: sqlite3.Connection) -> tuple[str, str | None]:
    """没有用户时创建默认管理员，返回 (用户名, 明文密码或 None)。"""
    if count_users(conn) > 0:
        return str(config.get("security.default_admin_user", "admin")), None
    username = str(config.get("security.default_admin_user", "admin") or "admin")
    password = generate_password(16, symbols=False)
    must_change = bool(config.get("security.force_password_change", False))
    create_user(conn, username, password, role="admin", must_change=must_change)
    record_credentials_file(config, username, password)
    return username, password


def record_credentials_file(config, username: str, password: str) -> None:
    """把首次生成的账号写到 0600 文件，方便用户查看；建议登录后删除。"""
    try:
        config.ensure_layout()
        path = config.data_dir / "initial-admin.txt"
        text = (
            "# HP 4928 打印服务 首次生成的默认账号\n"
            f"# 生成时间: {utcnow()}\n"
            f"用户名: {username}\n"
            f"密码:   {password}\n"
            "# 登录后请在「账号」页修改密码，然后删除本文件。\n"
            "# 忘记密码可用: python3 -m hp4928.cli passwd --user admin\n"
        )
        path.write_text(text, encoding="utf-8")
        import os

        if os.name == "posix":
            os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------- 登录
def _client_ip(cfg, req: Request | None = None) -> str:
    req = req or request
    if cfg.get("server.trust_proxy", False):
        forwarded = req.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return req.remote_addr or "unknown"


def _window_start(cfg) -> float:
    seconds = int(cfg.get("security.login_window_seconds", 300) or 300)
    return time.time() - max(30, seconds)


def record_attempt(conn: sqlite3.Connection, ip: str, username: str, ok: bool) -> None:
    execute(
        conn,
        "INSERT INTO login_attempts(ip, username, at, ok) VALUES(?, ?, ?, ?)",
        (ip, username, time.time(), int(ok)),
    )
    # 顺手清理 7 天前的记录，避免表无限增长
    execute(conn, "DELETE FROM login_attempts WHERE at < ?", (time.time() - 7 * 86400,))


def failure_count(conn: sqlite3.Connection, ip: str, username: str, cfg) -> int:
    since = _window_start(cfg)
    row = query_one(
        conn,
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE ok = 0 AND at >= ? AND (ip = ? OR lower(username) = lower(?))",
        (since, ip, username or ""),
    )
    return int(row["n"]) if row else 0


def authenticate(conn: sqlite3.Connection, cfg, username: str, password: str, ip: str) -> Principal:
    """校验账号密码，带失败次数限制。"""
    max_attempts = int(cfg.get("security.login_max_attempts", 5) or 5)
    window = max(30, int(cfg.get("security.login_window_seconds", 300) or 300))

    if failure_count(conn, ip, username, cfg) >= max_attempts:
        record_attempt(conn, ip, username, False)
        raise AuthError(
            "尝试次数过多，请稍后再试", 429, retry_after=window
        )

    user = get_user(conn, username or "")
    if not user or not verify_password(user["password_hash"], password or ""):
        record_attempt(conn, ip, username, False)
        audit(conn, username or "-", "login.failed", f"ip={ip}")
        raise AuthError("用户名或密码不正确", 401)
    if user["disabled"]:
        record_attempt(conn, ip, username, False)
        raise AuthError("账号已停用", 403)

    record_attempt(conn, ip, username, True)
    execute(conn, "UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), user["id"]))
    audit(conn, user["username"], "login.ok", f"ip={ip}")
    return Principal(
        id=int(user["id"]),
        username=user["username"],
        role=user["role"],
        must_change=bool(user["must_change"]),
        disabled=bool(user["disabled"]),
    )


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: str = "") -> None:
    try:
        execute(
            conn,
            "INSERT INTO audit(at, actor, action, detail) VALUES(?, ?, ?, ?)",
            (utcnow(), actor, action, detail[:2000]),
        )
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------- 会话
def current_principal() -> Principal | None:
    """从会话里取当前用户（每次都核对数据库，停用即失效）。"""
    uid = session.get("uid")
    if not uid:
        return None
    store = current_app.extensions["hp4928"]
    row = get_user_by_id(store.conn, int(uid))
    if not row or row["disabled"]:
        session.clear()
        return None
    return Principal(
        id=int(row["id"]),
        username=row["username"],
        role=row["role"],
        must_change=bool(row["must_change"]),
        disabled=bool(row["disabled"]),
    )


def login_principal(principal: Principal) -> None:
    session.clear()
    session["uid"] = principal.id
    session["user"] = principal.username
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def logout_principal() -> None:
    session.clear()


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def csrf_ok(req: Request) -> bool:
    expected = session.get("csrf") or ""
    supplied = req.headers.get("X-CSRF-Token") or req.form.get("_csrf") or ""
    if not expected or not supplied:
        return False
    return hmac.compare_digest(str(expected), str(supplied))


# --------------------------------------------------------------------- 装饰器
def _wants_json(req: Request) -> bool:
    if req.path.startswith("/api/"):
        return True
    return "application/json" in (req.headers.get("Accept") or "")


def login_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        principal = current_principal()
        if principal is None:
            if _wants_json(request):
                return jsonify({"ok": False, "error": "请先登录", "code": "auth_required"}), 401
            return redirect(url_for("web.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        principal = current_principal()
        if principal is None:
            if _wants_json(request):
                return jsonify({"ok": False, "error": "请先登录", "code": "auth_required"}), 401
            return redirect(url_for("web.login", next=request.full_path))
        if not principal.is_admin:
            if _wants_json(request):
                return jsonify({"ok": False, "error": "需要管理员权限", "code": "forbidden"}), 403
            return redirect(url_for("web.index"))
        return view(*args, **kwargs)

    return wrapper


def install_csrf(app: Flask, exempt_endpoints: set[str] | None = None) -> None:
    """给所有写请求加 CSRF 校验（登录接口默认豁免，方便脚本调用）。"""
    exempt = set(exempt_endpoints or {"api.login"})

    @app.before_request
    def _csrf_guard():  # type: ignore[misc]
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        if request.endpoint in exempt:
            return None
        # 未登录的请求交给鉴权层返回 401，不在这里报 CSRF
        if not session.get("uid"):
            return None
        if not csrf_ok(request):
            audit(current_app.extensions["hp4928"].conn, "-", "csrf.reject", request.path)
            return (
                jsonify({"ok": False, "error": "CSRF 校验失败，请刷新页面重试", "code": "csrf"}),
                400,
            )
        return None


def register_blueprint(app: Flask) -> Blueprint:
    """账号相关 API。"""
    bp = Blueprint("api_auth", __name__)

    @bp.post("/api/auth/login")
    def login():
        cfg = current_app.extensions["hp4928"].cfg
        conn = current_app.extensions["hp4928"].conn
        payload = request.get_json(silent=True) or request.form or {}
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        try:
            principal = authenticate(conn, cfg, username, password, _client_ip(cfg))
        except AuthError as exc:
            response = jsonify({"ok": False, "error": exc.message, "code": "auth_failed"})
            if exc.retry_after:
                response.headers["Retry-After"] = str(exc.retry_after)
            return response, exc.code
        login_principal(principal)
        return jsonify(
            {
                "ok": True,
                "user": principal.to_public(),
                "csrf": session.get("csrf"),
                "redirect": url_for("web.index"),
            }
        )

    @bp.post("/api/auth/logout")
    def logout():
        store = current_app.extensions["hp4928"]
        principal = current_principal()
        if principal:
            audit(store.conn, principal.username, "logout")
        logout_principal()
        return jsonify({"ok": True, "redirect": url_for("web.login")})

    @bp.get("/api/auth/me")
    def me():
        principal = current_principal()
        if principal is None:
            return jsonify({"ok": False, "error": "未登录", "code": "auth_required"}), 401
        return jsonify({"ok": True, "user": principal.to_public(), "csrf": csrf_token()})

    @bp.post("/api/auth/password")
    @login_required
    def change_password():
        store = current_app.extensions["hp4928"]
        principal = current_principal()
        assert principal is not None
        payload = request.get_json(silent=True) or request.form or {}
        current_password = str(payload.get("current_password", ""))
        new_password = str(payload.get("new_password", ""))
        user = get_user(store.conn, principal.username) or {}
        if not verify_password(user.get("password_hash", ""), current_password):
            raise AuthError("当前密码不正确", 400)
        set_password(
            store.conn,
            principal.username,
            new_password,
            must_change=False,
            minimum=int(store.cfg.get("security.min_password_length", 8) or 8),
        )
        audit(store.conn, principal.username, "password.change")
        return jsonify({"ok": True, "message": "密码已更新"})

    return bp
