"""JSON API。"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request, send_file

from . import auth, docs, media as media_mod, printers, settings as settings_mod
from .auth import admin_required, audit, current_principal, login_required
from .jobs import JobError
from .proc import which
from .store import Store

bp = Blueprint("api", __name__)

UPLOAD_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")
_UNSAFE_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def store() -> Store:
    return current_app.extensions["hp4928"]


def _json_error(message: str, code: int = 400, **extra: Any):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return jsonify(payload), code


def safe_filename(name: str) -> str:
    """保留中文，去掉路径分隔符与控制字符。"""
    cleaned = _UNSAFE_NAME_RE.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    cleaned = cleaned[-120:] if len(cleaned) > 120 else cleaned
    return cleaned or "upload"


def clean_title(text: str, fallback: str = "打印任务") -> str:
    cleaned = _CONTROL_RE.sub("", text or "").strip()
    return cleaned[:120] or fallback


def find_upload(cfg, token: str) -> Path | None:
    if not UPLOAD_TOKEN_RE.match(token or ""):
        return None
    for candidate in cfg.upload_dir.glob(f"{token}_*"):
        if candidate.is_file():
            return candidate
    return None


def _require_upload(token: str) -> Path:
    path = find_upload(store().cfg, token)
    if path is None:
        raise docs.DocumentError("上传文件不存在或已过期，请重新上传")
    return path


def _can_access(job: dict[str, Any], principal) -> bool:
    if principal is None:
        return False
    return principal.is_admin or job.get("owner_id") == principal.id


# --------------------------------------------------------------------- 基础
@bp.get("/api/healthz")
def healthz():
    return jsonify({"ok": True, "version": store().version, "time": __import__("time").time()})


@bp.get("/api/status")
@login_required
def api_status():
    return jsonify({"ok": True, **store().status()})


@bp.get("/api/schema")
@login_required
def api_schema():
    refresh = request.args.get("refresh") in {"1", "true", "yes"}
    return jsonify({"ok": True, "schema": store().schema(refresh=refresh)})


@bp.get("/api/doctor")
@admin_required
def api_doctor():
    checks = store().doctor()
    worst = "ok"
    for item in checks:
        if item["status"] == "fail":
            worst = "fail"
            break
        if item["status"] == "warn":
            worst = "warn"
    return jsonify({"ok": worst != "fail", "status": worst, "checks": checks})


@bp.get("/api/media")
@login_required
def api_media():
    include_all = request.args.get("all") in {"1", "true", "yes"}
    items = media_mod.list_media(include_unsupported=include_all)
    return jsonify({"ok": True, "media": [item.to_public() for item in items]})


# --------------------------------------------------------------------- 打印机 / 队列
@bp.get("/api/printers")
@login_required
def api_printers():
    """CUPS 队列列表 + 当前选中的是哪个 + 能不能管理。"""
    state = store()
    cfg = state.cfg
    selected = state.selected_queue()
    queues = printers.list_queues(cfg)
    return jsonify(
        {
            "ok": True,
            "selected": selected,
            "configured": str(cfg.get("printer.name", "") or ""),
            "can_manage": printers.can_manage_queues(cfg),
            "manage_hint": printers.manage_hint(cfg),
            "cups_available": printers.cups_available(cfg),
            "queues": [item.to_public() for item in queues],
            "printers": [],  # 由 /api/printers/discover 提供
        }
    )


@bp.get("/api/printers/discover")
@admin_required
def api_discover_printers():
    """发现局域网里可用的打印机（lpinfo + ippfind）。"""
    cfg = store().cfg
    try:
        found = printers.discover_network(cfg, scan_seconds=4)
    except Exception as exc:  # noqa: BLE001
        return _json_error(f"探测失败：{exc}", 500)
    notes = []
    if not which("ippfind"):
        notes.append("未安装 cups-ipp-utils，无法用 mDNS 主动扫描；"
                     "只能看到 CUPS 已知的网络设备。安装：sudo apt install -y cups-ipp-utils")
    if not found:
        notes.append("没有发现局域网打印机。确认打印机已联网、与本机同一网段，"
                     "或直接手动填地址 ipp://打印机IP/ipp/print")
    return jsonify(
        {
            "ok": True,
            "printers": [item.to_public() for item in found],
            "queues": [item.to_public() for item in printers.list_queues(cfg)],
            "selected": store().selected_queue(),
            "notes": notes,
        }
    )


@bp.post("/api/printers/select")
@admin_required
def api_select_printer():
    """切换服务使用的队列；传空字符串表示恢复「配置里的 printer.name」。"""
    state = store()
    cfg = state.cfg
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if name:
        queues = printers.list_queues(cfg)
        if not any(item.name == name for item in queues):
            return _json_error(f"CUPS 里没有队列 {name}", 404)
    state.set_selected_queue(name)
    audit(state.conn, current_principal().username if current_principal() else "-",
          "printer.select", name or "(auto)")
    return jsonify({"ok": True, "selected": state.selected_queue(), "status": state.status()})


@bp.post("/api/printers/add")
@admin_required
def api_add_printer():
    state = store()
    cfg = state.cfg
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    uri = str(payload.get("uri") or "").strip()
    model = str(payload.get("model") or "").strip()
    select = bool(payload.get("select", True))
    try:
        result = printers.add_queue(cfg, name, uri, model)
    except printers.BackendError as exc:
        return _json_error(str(exc), 400)
    if select:
        state.set_selected_queue(name)
    audit(state.conn, current_principal().username if current_principal() else "-",
          "printer.add", f"{name} {uri}")
    return jsonify(
        {
            "ok": True,
            **result,
            "selected": state.selected_queue(),
            "status": state.status(),
            "queues": [item.to_public() for item in printers.list_queues(cfg)],
        }
    )


@bp.delete("/api/printers/<name>")
@admin_required
def api_delete_printer(name: str):
    state = store()
    if name == state.selected_queue():
        return _json_error("这是当前正在使用的队列，请先切换到别的队列再删除", 409)
    try:
        result = printers.delete_queue(state.cfg, name)
    except printers.BackendError as exc:
        return _json_error(str(exc), 400)
    audit(state.conn, current_principal().username if current_principal() else "-",
          "printer.delete", name)
    return jsonify({"ok": True, **result})


@bp.post("/api/printers/test")
@admin_required
def api_test_printer():
    """往指定队列打一张测试页，验证连接确实通。"""
    from .cups import CupsPrinter
    from .cups import PrintError as CupsPrintError

    state = store()
    cfg = state.cfg
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip() or state.selected_queue()
    if not name:
        return _json_error("没有可测试的队列", 400)
    if not printers.cups_available(cfg):
        return _json_error("CUPS 客户端不可用（缺 lp/lpstat），无法测试真机连接", 400)
    if not any(item.name == name for item in printers.list_queues(cfg)):
        return _json_error(f"CUPS 里没有队列 {name}", 404)

    work = cfg.data_dir / "self-test"
    pdf = docs.make_selftest_pdf(
        work / f"connect-{int(time.time())}.pdf",
        pages=1,
        note=f"queue: {name}",
    )
    try:
        result = CupsPrinter(cfg, name).submit(pdf, [], f"connectivity test {name}", 1)
    except CupsPrintError as exc:
        return _json_error(f"测试页投递失败：{exc}", 400)
    audit(state.conn, current_principal().username if current_principal() else "-",
          "printer.test", name)
    return jsonify(
        {
            "ok": True,
            "queue": name,
            "cups_job": result.job_id,
            "command": result.command,
            "message": result.message,
        }
    )


# --------------------------------------------------------------------- 上传
@bp.post("/api/upload")
@login_required
def api_upload():
    cfg = store().cfg
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _json_error("没有收到文件", 400)

    token = uuid.uuid4().hex
    name = safe_filename(upload.filename)
    cfg.upload_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.upload_dir / f"{token}_{name}"
    upload.save(target)

    try:
        kind = docs.sniff_kind(target, name)
    except docs.DocumentError as exc:
        target.unlink(missing_ok=True)
        return _json_error(str(exc), 415)

    info: dict[str, Any] = {
        "upload_id": token,
        "name": name,
        "kind": kind,
        "bytes": target.stat().st_size,
        "pages": 0,
        "mode_suggestion": "photo" if kind == docs.KIND_IMAGE else "document",
    }
    try:
        if kind == docs.KIND_PDF:
            info["pages"] = docs.page_count(target, cfg)
            width_pt, height_pt = docs.pdf_page_size(target)
            info["page_mm"] = [round(media_mod.pt_to_mm(width_pt), 1),
                               round(media_mod.pt_to_mm(height_pt), 1)]
        elif kind == docs.KIND_IMAGE:
            info["pages"] = 1
            from PIL import Image

            with Image.open(target) as image:
                info["image_size"] = list(image.size)
    except docs.DocumentError as exc:
        target.unlink(missing_ok=True)
        return _json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        return _json_error(f"文件解析失败：{exc}", 400)

    principal = current_principal()
    info["preview_url"] = ""
    if kind in {docs.KIND_PDF, docs.KIND_WORD, docs.KIND_IMAGE}:
        info["preview_url"] = f"/api/uploads/{token}/preview"
    audit(store().conn, principal.username if principal else "-", "upload", f"{name} ({kind})")
    return jsonify({"ok": True, "upload": info})


@bp.get("/api/uploads/<token>/preview")
@login_required
def api_upload_preview(token: str):
    cfg = store().cfg
    path = find_upload(cfg, token)
    if path is None:
        return _json_error("文件不存在", 404)
    try:
        kind = docs.sniff_kind(path, path.name)
    except docs.DocumentError as exc:
        return _json_error(str(exc), 400)
    thumb_dir = cfg.data_dir / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb = thumb_dir / f"{token}.png"
    if thumb.is_file():
        return send_file(thumb, mimetype="image/png")

    if kind == docs.KIND_IMAGE:
        try:
            from PIL import Image, ImageOps

            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((720, 720))
                image.convert("RGB").save(thumb, "PNG")
            return send_file(thumb, mimetype="image/png")
        except Exception:  # noqa: BLE001
            return _json_error("无法生成预览", 415)
    if kind == docs.KIND_PDF:
        if docs.first_page_thumbnail(path, thumb, cfg):
            return send_file(thumb, mimetype="image/png")
        return _json_error("服务器未安装 poppler-utils，无法预览", 415)
    return _json_error("该格式暂不支持预览", 415)


# --------------------------------------------------------------------- 任务
@bp.post("/api/jobs")
@login_required
def api_create_job():
    principal = current_principal()
    assert principal is not None
    cfg = store().cfg
    payload = request.get_json(silent=True) or {}

    token = str(payload.get("upload_id") or "").strip()
    if not token:
        return _json_error("缺少 upload_id，请先上传文件", 400)
    try:
        path = _require_upload(token)
        kind = docs.sniff_kind(path, path.name)
    except docs.DocumentError as exc:
        return _json_error(str(exc), 400)

    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in {"document", "photo"}:
        mode = "photo" if kind == docs.KIND_IMAGE else "document"

    catalog = store().catalog()
    try:
        settings, warnings = settings_mod.normalize_settings(
            payload.get("settings") or {}, cfg, catalog, mode
        )
    except settings_mod.SettingsError as exc:
        return _json_error(str(exc), 400)

    # 提前校验页范围，避免任务排队后才失败
    if kind == docs.KIND_PDF:
        try:
            total = docs.page_count(path, cfg)
            docs.parse_page_ranges(settings["page_ranges"], total)
        except docs.DocumentError as exc:
            return _json_error(str(exc), 400)
    elif kind == docs.KIND_IMAGE and settings["page_ranges"].strip():
        return _json_error("单张图片不需要页码范围", 400)

    original_name = path.name.split("_", 1)[-1] if "_" in path.name else path.name
    title = clean_title(str(payload.get("title") or ""), Path(original_name).stem)

    try:
        job = store().jobs.create(
            owner={"id": principal.id, "username": principal.username},
            title=title,
            source_path=path,
            source_name=path.name.split("_", 1)[-1],
            kind=kind,
            settings=settings,
            source_bytes=path.stat().st_size,
        )
    except Exception as exc:  # noqa: BLE001
        return _json_error(f"创建任务失败：{exc}", 500)

    audit(store().conn, principal.username, "job.create",
          f"{job['id']} {title} {settings_mod.describe(settings)}")
    return jsonify(
        {
            "ok": True,
            "job": store().jobs.public(job),
            "settings": settings,
            "warnings": warnings,
        }
    )


@bp.get("/api/jobs")
@login_required
def api_jobs():
    principal = current_principal()
    assert principal is not None
    limit = request.args.get("limit", "50")
    try:
        limit_value = max(1, min(200, int(limit)))
    except ValueError:
        limit_value = 50
    owner = None if principal.is_admin else principal.id
    jobs = store().jobs.list(limit=limit_value, owner_id=owner)
    return jsonify(
        {
            "ok": True,
            "jobs": [store().jobs.public(job) for job in jobs],
            "stats": store().jobs.stats(),
        }
    )


@bp.get("/api/jobs/<job_id>")
@login_required
def api_job(job_id: str):
    principal = current_principal()
    job = store().jobs.get(job_id)
    if not job:
        return _json_error("任务不存在", 404)
    if not _can_access(job, principal):
        return _json_error("无权查看该任务", 403)
    return jsonify({"ok": True, "job": store().jobs.public(job)})


@bp.post("/api/jobs/<job_id>/continue")
@login_required
def api_job_continue(job_id: str):
    """手动双面的「继续打印」按钮。没有这个请求，第二面永远不会打印。"""
    principal = current_principal()
    assert principal is not None
    job = store().jobs.get(job_id)
    if not job:
        return _json_error("任务不存在", 404)
    if not _can_access(job, principal):
        return _json_error("无权操作该任务", 403)
    try:
        updated = store().jobs.continue_job(job_id, principal.username)
    except JobError as exc:
        return _json_error(exc.message, exc.code)
    audit(store().conn, principal.username, "job.continue", job_id)
    return jsonify({"ok": True, "job": store().jobs.public(updated)})


@bp.post("/api/jobs/<job_id>/cancel")
@login_required
def api_job_cancel(job_id: str):
    principal = current_principal()
    assert principal is not None
    job = store().jobs.get(job_id)
    if not job:
        return _json_error("任务不存在", 404)
    if not _can_access(job, principal):
        return _json_error("无权操作该任务", 403)
    try:
        updated = store().jobs.cancel(job_id, principal.username)
    except JobError as exc:
        return _json_error(exc.message, exc.code)
    audit(store().conn, principal.username, "job.cancel", job_id)
    return jsonify({"ok": True, "job": store().jobs.public(updated)})


@bp.post("/api/jobs/<job_id>/retry")
@login_required
def api_job_retry(job_id: str):
    principal = current_principal()
    assert principal is not None
    job = store().jobs.get(job_id)
    if not job:
        return _json_error("任务不存在", 404)
    if not _can_access(job, principal):
        return _json_error("无权操作该任务", 403)
    try:
        updated = store().jobs.retry(job_id, principal.username)
    except JobError as exc:
        return _json_error(exc.message, exc.code)
    audit(store().conn, principal.username, "job.retry", job_id)
    return jsonify({"ok": True, "job": store().jobs.public(updated)})


@bp.get("/api/jobs/<job_id>/file/<which>")
@login_required
def api_job_file(job_id: str, which: str):
    principal = current_principal()
    job = store().jobs.get(job_id)
    if not job:
        return _json_error("任务不存在", 404)
    if not _can_access(job, principal):
        return _json_error("无权下载该文件", 403)
    mapping = {
        "source": job.get("source_path"),
        "pass1": job.get("pass1_path"),
        "pass2": job.get("pass2_path"),
    }
    if which not in mapping:
        return _json_error("未知文件类型", 400)
    path = Path(str(mapping[which] or ""))
    if not path.is_file():
        return _json_error("文件不存在或已被清理", 404)
    download_name = f"{job_id}-{which}{path.suffix}"
    return send_file(path, as_attachment=True, download_name=download_name)


@bp.get("/api/jobs/<job_id>/preview")
@login_required
def api_job_preview(job_id: str):
    principal = current_principal()
    job = store().jobs.get(job_id)
    if not job:
        return _json_error("任务不存在", 404)
    if not _can_access(job, principal):
        return _json_error("无权查看该任务", 403)
    which = request.args.get("which", "pass1")
    source = Path(str(job.get("pass1_path" if which == "pass1" else "pass2_path") or ""))
    if not source.is_file():
        source = Path(str(job.get("source_path") or ""))
    if not source.is_file():
        return _json_error("文件不存在", 404)
    thumb_dir = store().cfg.data_dir / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb = thumb_dir / f"{job_id}-{which}.png"
    if not thumb.is_file():
        if docs.first_page_thumbnail(source, thumb, store().cfg) is None:
            # Word 源文件无法直接预览，先转一份 PDF
            try:
                pdf = docs.ensure_pdf(source, thumb_dir / job_id, store().cfg, job["kind"], job_id)
                docs.first_page_thumbnail(pdf, thumb, store().cfg)
            except docs.DocumentError:
                return _json_error("无法生成预览", 415)
    if thumb.is_file():
        return send_file(thumb, mimetype="image/png")
    return _json_error("无法生成预览", 415)


# --------------------------------------------------------------------- 虚拟打印
@bp.get("/api/spool")
@login_required
def api_spool():
    files = printers.spool_files(store().cfg)
    return jsonify(
        {
            "ok": True,
            "dir": str(store().cfg.spool_dir),
            "files": [
                {
                    "name": item.name,
                    "bytes": item.stat().st_size,
                    "modified": int(item.stat().st_mtime),
                    "url": f"/api/spool/{item.name}",
                }
                for item in files[:50]
            ],
        }
    )


@bp.get("/api/spool/<name>")
@login_required
def api_spool_file(name: str):
    if not re.fullmatch(r"[\w\-.]+\.pdf", name or ""):
        return _json_error("文件名不合法", 400)
    path = store().cfg.spool_dir / name
    if not path.is_file():
        return _json_error("文件不存在", 404)
    return send_file(path, as_attachment=True, download_name=name)


# --------------------------------------------------------------------- 账号
@bp.get("/api/users")
@admin_required
def api_users():
    users = auth.list_users(store().conn)
    return jsonify({"ok": True, "users": users})


@bp.post("/api/users")
@admin_required
def api_create_user():
    principal = current_principal()
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()
    role = str(payload.get("role") or "user").strip().lower()
    generate = bool(payload.get("generate"))
    cfg = store().cfg
    try:
        auth.validate_username(username)
        if generate or not password:
            password = auth.generate_password(14, symbols=False)
        auth.validate_new_password(password, int(cfg.get("security.min_password_length", 8) or 8))
        user = auth.create_user(store().conn, username, password, role=role)
    except auth.AuthError as exc:
        return _json_error(exc.message, exc.code)
    audit(store().conn, principal.username if principal else "-", "user.create", username)
    return jsonify(
        {
            "ok": True,
            "user": {key: user[key] for key in ("id", "username", "role") if key in user},
            "password": password if generate or not payload.get("password") else "",
        }
    )


@bp.post("/api/users/<username>/password")
@admin_required
def api_reset_password(username: str):
    principal = current_principal()
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password") or "").strip()
    generate = bool(payload.get("generate", True))
    cfg = store().cfg
    try:
        if generate or not password:
            password = auth.generate_password(14, symbols=False)
        auth.set_password(
            store().conn, username, password,
            must_change=False,
            minimum=int(cfg.get("security.min_password_length", 8) or 8),
        )
    except auth.AuthError as exc:
        return _json_error(exc.message, exc.code)
    audit(store().conn, principal.username if principal else "-", "user.password", username)
    return jsonify({"ok": True, "username": username, "password": password})


@bp.delete("/api/users/<username>")
@admin_required
def api_delete_user(username: str):
    principal = current_principal()
    if principal and principal.username.lower() == username.lower():
        return _json_error("不能删除当前登录的账号", 400)
    try:
        auth.delete_user(store().conn, username)
    except auth.AuthError as exc:
        return _json_error(exc.message, exc.code)
    audit(store().conn, principal.username if principal else "-", "user.delete", username)
    return jsonify({"ok": True})


@bp.post("/api/users/<username>/disable")
@admin_required
def api_disable_user(username: str):
    principal = current_principal()
    payload = request.get_json(silent=True) or {}
    disabled = bool(payload.get("disabled", True))
    if principal and principal.username.lower() == username.lower() and disabled:
        return _json_error("不能停用当前登录的账号", 400)
    try:
        auth.set_disabled(store().conn, username, disabled)
    except auth.AuthError as exc:
        return _json_error(exc.message, exc.code)
    return jsonify({"ok": True})


@bp.get("/api/audit")
@admin_required
def api_audit():
    rows = store().conn.execute(
        "SELECT at, actor, action, detail FROM audit ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@bp.get("/api/config")
@admin_required
def api_config():
    """只读展示关键配置（密码等敏感项不返回）。"""
    cfg = store().cfg
    return jsonify(
        {
            "ok": True,
            "config": {
                "host": cfg.host,
                "port": cfg.port,
                "data_dir": str(cfg.data_dir),
                "sources": [str(item) for item in cfg.sources],
                "backend": cfg.get("printer.backend"),
                "printer": cfg.get("printer.name"),
                "usb_require_device": cfg.get("printer.usb.require_device"),
                "retention_days": cfg.get("retention.days"),
                "max_upload_mb": cfg.get("server.max_upload_mb"),
                "trust_proxy": cfg.get("server.trust_proxy"),
                "allowed_cidrs": cfg.get("server.allowed_cidrs"),
            },
        }
    )
