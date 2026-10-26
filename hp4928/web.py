"""Flask 应用工厂与页面路由。"""

from __future__ import annotations

import ipaddress
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import __version__, api, auth, docs, printers
from .auth import (
    AuthError,
    authenticate,
    current_principal,
    install_csrf,
    login_principal,
    login_required,
    admin_required,
    logout_principal,
)
from .config import load_config
from .jobs import JobError, STATE_LABELS, STATE_AWAITING_FLIP, STATE_DONE, STATE_FAILED
from .settings import SettingsError
from .store import Store

log = logging.getLogger("hp4928")

views = Blueprint("web", __name__)


@views.app_template_filter("localtime")
def localtime_filter(value: str | None) -> str:
    return _to_local(value)

# 侧边栏导航（顺序即显示顺序；admin=True 仅管理员可见）
NAV_ITEMS = (
    {"endpoint": "web.index", "label": "工作台", "icon": "dashboard", "admin": False},
    {"endpoint": "web.account_page", "label": "个人中心", "icon": "user", "admin": False},
    {"endpoint": "web.notifications_page", "label": "消息通知", "icon": "bell", "admin": False},
    {"endpoint": "web.settings_page", "label": "系统管理", "icon": "settings", "admin": True},
    {"endpoint": "web.users_page", "label": "用户权限", "icon": "users", "admin": True},
)

# 不在导航里、但顶栏要显示标题的页面
EXTRA_TITLES = {
    "web.history": "任务历史",
    "web.settings_doctor": "环境自检",
    "web.login": "登录",
}


def _to_local(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """数据库里存的是 UTC（...Z），展示时转成服务器本地时间。"""
    if not value:
        return ""
    from datetime import datetime, timezone

    text = str(value)
    try:
        stamp = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return text
    return stamp.astimezone().strftime(fmt)


def _short_time(value: str | None) -> str:
    return _to_local(value, "%m-%d %H:%M")


# --------------------------------------------------------------------- 模板上下文
@views.app_context_processor
def inject_globals() -> dict[str, Any]:
    store: Store = current_app.extensions["hp4928"]
    principal = current_principal()

    nav_badges: dict[str, int] = {}
    if principal is not None:
        try:
            sql = ("SELECT COUNT(*) AS n FROM jobs "
                   "WHERE state IN ('awaiting_flip', 'failed')")
            params: tuple = ()
            if not principal.is_admin:
                sql += " AND owner_id = ?"
                params = (principal.id,)
            row = store.conn.execute(sql, params).fetchone()
            pending = int(row["n"]) if row else 0
            if pending:
                nav_badges["web.notifications_page"] = pending
        except Exception:  # noqa: BLE001 - 徽标不能拖垮页面
            pass

    titles = {item["endpoint"]: item["label"] for item in NAV_ITEMS}
    titles.update(EXTRA_TITLES)
    nav_title = titles.get(request.endpoint or "", store.cfg.get("app.site_name", "打印服务"))

    return {
        "site_name": store.cfg.get("app.site_name", "打印服务"),
        "app_name": store.cfg.get("app.name", "HP 4928 打印服务"),
        "version": __version__,
        "user": principal,
        "csrf_token": auth.csrf_token(),
        "nav_items": NAV_ITEMS,
        "nav_badges": nav_badges,
        "nav_title": nav_title,
    }


# --------------------------------------------------------------------- 页面
@views.get("/login")
def login():
    if current_principal() is not None:
        return redirect(url_for("web.index"))
    return render_template(
        "login.html",
        next_url=request.args.get("next", ""),
        urls=current_app.extensions["hp4928"].cfg.lan_urls(),
    )


@views.post("/login")
def login_submit():
    """无 JS 也能登录的表单提交。"""
    cfg = current_app.extensions["hp4928"].cfg
    conn = current_app.extensions["hp4928"].conn
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    try:
        principal = authenticate(conn, cfg, username, password, request.remote_addr or "unknown")
    except AuthError as exc:
        return render_template(
            "login.html",
            error=exc.message,
            username=username,
            urls=cfg.lan_urls(),
        ), exc.code
    login_principal(principal)
    target = request.form.get("next") or url_for("web.index")
    if not str(target).startswith("/"):
        target = url_for("web.index")
    return redirect(target)


@views.post("/logout")
@login_required
def logout():
    principal = current_principal()
    if principal:
        auth.audit(current_app.extensions["hp4928"].conn, principal.username, "logout")
    logout_principal()
    return redirect(url_for("web.login"))


@views.get("/")
@login_required
def index():
    store: Store = current_app.extensions["hp4928"]
    principal = current_principal()
    assert principal is not None
    # 普通用户只能看到自己的任务（管理员看全部）
    owner_id = None if principal.is_admin else principal.id
    bootstrap = {
        "schema": store.schema(),
        "status": store.status(),
        "jobs": [store.jobs.public(job) for job in store.jobs.list(limit=30, owner_id=owner_id)],
        "limits": {
            "max_upload_mb": int(store.cfg.get("server.max_upload_mb", 64) or 64),
            "session_hours": int(store.cfg.get("server.session_hours", 12) or 12),
        },
        "urls": store.cfg.lan_urls(),
    }
    return render_template(
        "index.html",
        bootstrap=bootstrap,
        state_labels=STATE_LABELS,
        max_upload_mb=bootstrap["limits"]["max_upload_mb"],
    )


@views.get("/history")
@login_required
def history():
    store: Store = current_app.extensions["hp4928"]
    principal = current_principal()
    assert principal is not None
    owner_id = None if principal.is_admin else principal.id
    return render_template(
        "history.html",
        jobs=[store.jobs.public(job)
              for job in store.jobs.list(limit=200, owner_id=owner_id)],
        state_labels=STATE_LABELS,
    )


@views.get("/account")
@login_required
def account_page():
    """个人中心：账号信息 + 改密 + 最近登录。"""
    store: Store = current_app.extensions["hp4928"]
    principal = current_principal()
    assert principal is not None
    profile = auth.get_user(store.conn, principal.username) or {}
    try:
        rows = store.conn.execute(
            "SELECT at, action, detail FROM audit WHERE actor = ? ORDER BY id DESC LIMIT 8",
            (principal.username,),
        ).fetchall()
        records = [dict(row) for row in rows]
    except Exception:  # noqa: BLE001
        records = []
    return render_template(
        "account.html",
        profile={
            "username": profile.get("username", principal.username),
            "role": profile.get("role", principal.role),
            "created_at": profile.get("created_at") or "",
            "last_login_at": profile.get("last_login_at") or "",
        },
        records=records,
        session_hours=int(store.cfg.get("server.session_hours", 12) or 12),
    )


@views.get("/notifications")
@login_required
def notifications_page():
    """消息通知：从任务状态实时推导（等待翻面 / 失败 / 最近完成）。"""
    store: Store = current_app.extensions["hp4928"]
    principal = current_principal()
    assert principal is not None
    jobs = store.jobs.list(limit=60, owner_id=None if principal.is_admin else principal.id)

    items: list[dict[str, Any]] = []
    for job in jobs:
        if job["state"] == STATE_AWAITING_FLIP:
            items.append({
                "kind": "warn",
                "title": "需要翻面确认",
                "body": (f"「{job['title']}」第一面（奇数页 {job['pass1_pages']} 页）已打印，"
                         "翻面后点按钮打印第二面。"),
                "job_id": job["id"],
                "action": "continue",
                "time": _short_time(job.get("updated_at")),
            })
        elif job["state"] == STATE_FAILED:
            items.append({
                "kind": "danger",
                "title": "任务失败",
                "body": f"「{job['title']}」：{job.get('error') or '未知错误'}",
                "job_id": job["id"],
                "action": "retry",
                "time": _short_time(job.get("updated_at")),
            })
    for job in [item for item in jobs if item["state"] == STATE_DONE][:5]:
        items.append({
            "kind": "ok",
            "title": "打印完成",
            "body": f"「{job['title']}」{job['pages']} 页已全部投递。",
            "job_id": job["id"],
            "action": "",
            "time": _short_time(job.get("finished_at") or job.get("updated_at")),
        })

    return render_template(
        "notifications.html",
        items=items,
        manual_duplex_timeout=store.cfg.get("print.manual_duplex_timeout_minutes", 60),
    )


@views.get("/users")
@admin_required
def users_page():
    store: Store = current_app.extensions["hp4928"]
    return render_template("users.html", users=auth.list_users(store.conn))


@views.get("/settings")
@admin_required
def settings_page():
    store: Store = current_app.extensions["hp4928"]
    schema = store.schema()
    spool = printers.spool_files(store.cfg)[:20]
    return render_template(
        "settings.html",
        schema=schema,
        status=store.status(),
        config=store.cfg,
        spool=spool,
        checks=None,
    )


@views.get("/settings/doctor")
@admin_required
def settings_doctor():
    store: Store = current_app.extensions["hp4928"]
    checks = store.doctor()
    return render_template(
        "doctor.html",
        checks=checks,
        schema=store.schema(),
        status=store.status(),
        users=auth.list_users(store.conn),
        config=store.cfg,
        spool=printers.spool_files(store.cfg)[:20],
    )


# --------------------------------------------------------------------- 应用工厂
def _setup_logging(cfg) -> None:
    level = str(cfg.get("logging.level", "INFO") or "INFO").upper()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    file_target = str(cfg.get("logging.file") or "").strip()
    if file_target:
        try:
            cfg.log_dir.mkdir(parents=True, exist_ok=True)
            target = Path(file_target)
            if not target.is_absolute():
                target = cfg.log_dir / target
            handlers.append(logging.FileHandler(target, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def _ip_allowed(cfg, ip: str) -> bool:
    allowed = cfg.get("server.allowed_cidrs") or []
    if not allowed:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in allowed:
        try:
            if address in ipaddress.ip_network(str(item), strict=False):
                return True
        except ValueError:
            continue
    return False


def create_app(
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    start_workers: bool = True,
    root: Path | None = None,
) -> Flask:
    if config_path:
        import os

        os.environ["HP4928_CONFIG"] = str(config_path)

    cfg = load_config(root, overrides)
    _setup_logging(cfg)

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config.update(
        SECRET_KEY=cfg.secret_key(),
        MAX_CONTENT_LENGTH=int(cfg.get("server.max_upload_mb", 64) or 64) * 1024 * 1024,
        SESSION_COOKIE_NAME="hp4928_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(cfg.get("server.behind_https", False)),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=int(cfg.get("server.session_hours", 12) or 12)),
        TEMPLATES_AUTO_RELOAD=bool(cfg.get("server.debug", False)),
    )
    try:
        app.json.ensure_ascii = False  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - 老版本 Flask
        app.config["JSON_AS_ASCII"] = False

    store = Store(cfg)
    app.extensions["hp4928"] = store
    store.bootstrap()
    if start_workers:
        store.jobs.start()
    app.extensions["hp4928_jobs_started"] = start_workers

    app.register_blueprint(views)
    app.register_blueprint(auth.register_blueprint(app))
    app.register_blueprint(api.bp)
    install_csrf(app, exempt_endpoints={"api_auth.login", "web.login_submit"})

    # ---------------------------------------------------------------- 钩子
    @app.before_request
    def _guard_network():  # type: ignore[misc]
        ip = request.remote_addr or ""
        if cfg.get("server.trust_proxy", False):
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                ip = forwarded.split(",")[0].strip()
        if not _ip_allowed(cfg, ip):
            auth.audit(store.conn, "-", "network.denied", ip)
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": f"来源地址 {ip} 不在允许的局域网段内",
                                "code": "forbidden"}), 403
            return render_template("error.html", code=403,
                                   message=f"来源地址 {ip} 不在允许的局域网段内"), 403
        return None

    @app.after_request
    def _security_headers(response):  # type: ignore[misc]
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ---------------------------------------------------------------- 错误
    def _wants_json() -> bool:
        return request.path.startswith("/api/") or "application/json" in (
            request.headers.get("Accept") or ""
        )

    def _handle(message: str, code: int):
        if _wants_json():
            return jsonify({"ok": False, "error": message}), code
        return render_template("error.html", code=code, message=message), code

    @app.errorhandler(AuthError)
    def _auth_error(exc: AuthError):  # type: ignore[misc]
        return _handle(exc.message, exc.code)

    @app.errorhandler(SettingsError)
    def _settings_error(exc: SettingsError):  # type: ignore[misc]
        return _handle(str(exc), 400)

    @app.errorhandler(docs.DocumentError)
    def _doc_error(exc: docs.DocumentError):  # type: ignore[misc]
        return _handle(str(exc), 400)

    @app.errorhandler(JobError)
    def _job_error(exc: JobError):  # type: ignore[misc]
        return _handle(exc.message, exc.code)

    @app.errorhandler(413)
    def _too_large(_exc):  # type: ignore[misc]
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return _handle(f"文件太大，上限 {limit} MB（可在配置 server.max_upload_mb 调整）", 413)

    @app.errorhandler(404)
    def _not_found(_exc):  # type: ignore[misc]
        return _handle("页面不存在", 404)

    @app.errorhandler(500)
    def _server_error(exc):  # type: ignore[misc]
        log.exception("服务器错误: %s", exc)
        return _handle("服务器内部错误，请查看服务日志", 500)

    @app.teardown_appcontext
    def _teardown(_exc):  # type: ignore[misc]
        # 连接按线程复用（waitress 线程池），这里不关闭，进程退出时由 store.stop() 处理
        return None

    log.info("HP 4928 打印服务 v%s 启动；配置：%s；数据目录：%s",
             __version__, cfg.describe_sources(), cfg.data_dir)
    return app
