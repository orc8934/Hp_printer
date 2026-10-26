"""打印任务：状态机、手动双面两遍流程、后台工作线程。

手动双面（HP 4928 无自动双面器，官方为「手动双面」）流程：

    queued → preparing → printing(1) → awaiting_flip ──用户点「继续打印」──→ printing(2) → done
                                          │
                                          └──超时/取消──→ canceled

关键约束：进入 awaiting_flip 后**绝不自动**打印第二遍，必须显式确认。
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import docs, ppd, printers, settings as settings_mod
from .cups import PrintError
from .db import Database, execute, query_one, row_to_dict, transaction, utcnow

STATE_QUEUED = "queued"
STATE_PREPARING = "preparing"
STATE_PRINTING = "printing"
STATE_AWAITING_FLIP = "awaiting_flip"
STATE_PRINTING_SECOND = "printing_second"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

ACTIVE_STATES = (STATE_QUEUED, STATE_PREPARING, STATE_PRINTING, STATE_AWAITING_FLIP,
                 STATE_PRINTING_SECOND)

STATE_LABELS = {
    STATE_QUEUED: "排队中",
    STATE_PREPARING: "正在准备文件",
    STATE_PRINTING: "正在打印第一面",
    STATE_AWAITING_FLIP: "等待翻面",
    STATE_PRINTING_SECOND: "正在打印第二面",
    STATE_DONE: "已完成",
    STATE_FAILED: "失败",
    STATE_CANCELED: "已取消",
}


class JobError(RuntimeError):
    """任务操作失败（消息给用户看，code 用于 HTTP 状态码）。"""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def new_job_id() -> str:
    return f"J{time.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"


def duplex_hint(settings: dict[str, Any]) -> str:
    duplex = settings.get("duplex", "off")
    if duplex == "short-edge":
        return (
            "第一面（奇数页）已送打印。等打印机完全停止、纸张输出后，"
            "把整叠纸沿**短边**翻转（像翻日历），保持纸叠顺序不变，再点「继续打印」。"
        )
    return (
        "第一面（奇数页）已送打印。等打印机完全停止、纸张输出后，"
        "把整叠纸沿**长边**翻转（像翻书），保持纸叠顺序不变，再点「继续打印」。"
    )


class JobManager:
    def __init__(self, cfg, database: Database, workers: int = 2) -> None:
        self.cfg = cfg
        self.db = database
        self.workers = max(1, int(workers))
        self.queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._worker_loop, name=f"hp4928-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        self._sweeper = threading.Thread(target=self._sweep_loop, name="hp4928-sweeper", daemon=True)
        self._sweeper.start()
        # 进程重启后，把中断的任务标为失败，避免永远卡在「准备中」
        try:
            execute(
                self.db.conn,
                "UPDATE jobs SET state=?, error=?, updated_at=? "
                "WHERE state IN (?, ?, ?)",
                (STATE_FAILED, "服务重启导致任务中断，请重新提交", utcnow(),
                 STATE_PREPARING, STATE_PRINTING, STATE_PRINTING_SECOND),
            )
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()
        if self._sweeper:
            self._sweeper.join(timeout=2)
            self._sweeper = None

    # ------------------------------------------------------------------ 查询
    def get(self, job_id: str) -> dict[str, Any] | None:
        row = query_one(self.db.conn, "SELECT * FROM jobs WHERE id = ?", (job_id,))
        return row_to_dict(row) or None

    def list(self, limit: int = 50, owner_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if owner_id is not None:
            sql += " WHERE owner_id = ?"
            params.append(owner_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        rows = self.db.conn.execute(sql, tuple(params)).fetchall()
        return [row_to_dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        rows = self.db.conn.execute(
            "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
        ).fetchall()
        counts = {str(row["state"]): int(row["n"]) for row in rows}
        counts["active"] = sum(counts.get(state, 0) for state in ACTIVE_STATES)
        return counts

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        *,
        owner: dict[str, Any],
        title: str,
        source_path: Path,
        source_name: str,
        kind: str,
        settings: dict[str, Any],
        source_bytes: int,
    ) -> dict[str, Any]:
        job_id = new_job_id()
        now = utcnow()
        execute(
            self.db.conn,
            "INSERT INTO jobs(id, owner_id, owner_name, title, source_name, source_path, "
            "source_ext, source_bytes, kind, settings_json, state, message, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                owner.get("id"),
                owner.get("username", ""),
                title or source_name,
                source_name,
                str(source_path),
                Path(source_name).suffix.lower(),
                int(source_bytes),
                kind,
                json.dumps(settings, ensure_ascii=False),
                STATE_QUEUED,
                "已排队",
                now,
                now,
            ),
        )
        self.queue.put(("first", job_id))
        job = self.get(job_id)
        assert job is not None
        return job

    # ------------------------------------------------------------------ 状态
    def _preferred_queue(self) -> str:
        """界面上选中的队列（DB 里），空则用配置。"""
        from .db import kv_get

        try:
            return kv_get(self.db.conn, "printer.queue", "")
        except Exception:  # noqa: BLE001
            return ""

    def _set_state(self, job_id: str, state: str, *, message: str = "", error: str = "",
                   expect: tuple[str, ...] | None = None, extra: dict[str, Any] | None = None) -> bool:
        fields = ["state = ?", "message = ?", "error = ?", "updated_at = ?"]
        params: list[Any] = [state, message, error, utcnow()]
        for key, value in (extra or {}).items():
            fields.append(f"{key} = ?")
            params.append(value)
        if state in {STATE_DONE, STATE_FAILED, STATE_CANCELED}:
            fields.append("finished_at = ?")
            params.append(utcnow())
        sql = f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?"
        params.append(job_id)
        if expect:
            placeholders = ", ".join("?" for _ in expect)
            sql += f" AND state IN ({placeholders})"
            params.extend(expect)
        cursor = execute(self.db.conn, sql, params)
        return cursor.rowcount > 0

    def _fail(self, job_id: str, message: str) -> None:
        self._set_state(job_id, STATE_FAILED, error=message, message=message)

    # ------------------------------------------------------------------ 工作线程
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                task, job_id = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if task == "first":
                    self._run_first(job_id)
                elif task == "second":
                    self._run_second(job_id)
            except Exception:  # noqa: BLE001 - 工作线程不能死
                traceback.print_exc()
                try:
                    self._fail(job_id, "内部错误，请查看服务日志")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                try:
                    self.queue.task_done()
                except Exception:  # noqa: BLE001
                    pass

    def _sweep_loop(self) -> None:
        ticks = 0
        while not self._stop.wait(60):
            ticks += 1
            try:
                self.sweep_stale()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            if ticks % 60 == 0:
                try:
                    printers.cleanup_old_files(self.cfg, self.db.conn)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()

    def sweep_stale(self) -> int:
        """长时间停在「等待翻面」的任务自动取消，避免纸张状态混乱。"""
        minutes = float(self.cfg.get("print.manual_duplex_timeout_minutes", 60) or 0)
        if minutes <= 0:
            return 0
        cutoff = time.time() - minutes * 60
        rows = self.db.conn.execute(
            "SELECT id, updated_at FROM jobs WHERE state = ?", (STATE_AWAITING_FLIP,)
        ).fetchall()
        canceled = 0
        for row in rows:
            try:
                stamp = datetime.strptime(str(row["updated_at"]), "%Y-%m-%dT%H:%M:%SZ")
                stamp = stamp.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if stamp.timestamp() < cutoff:
                if self._set_state(
                    row["id"], STATE_CANCELED,
                    message=f"等待翻面超过 {minutes:g} 分钟，已自动取消（未打印第二面）",
                    expect=(STATE_AWAITING_FLIP,),
                ):
                    canceled += 1
        return canceled

    # ------------------------------------------------------------------ 第一遍
    def _run_first(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job or job["state"] != STATE_QUEUED:
            return
        if not self._set_state(job_id, STATE_PREPARING, message="正在转换与排版…",
                               expect=(STATE_QUEUED,)):
            return

        try:
            settings = json.loads(job["settings_json"] or "{}")
            backend = printers.select_backend(self.cfg, preferred_queue=self._preferred_queue())
            catalog = ppd.load_catalog(self.cfg, backend.queue or str(self.cfg.get("printer.name")))
            roles = ppd.resolve_roles(catalog)
            printer = printers.printer_for(self.cfg, backend)

            work = self.cfg.prepared_dir / job_id
            work.mkdir(parents=True, exist_ok=True)
            source = Path(job["source_path"])
            if not source.is_file():
                raise docs.DocumentError("原始文件已丢失（可能被清理），请重新上传")

            # 1) 生成可打印 PDF
            if settings.get("mode") == "photo" or job["kind"] == docs.KIND_IMAGE:
                print_pdf = self._prepare_photo(job, source, settings, work)
            else:
                print_pdf = docs.ensure_pdf(source, work, self.cfg, job["kind"], job_id)

            total_pages = docs.page_count(print_pdf, self.cfg)

            # 2) 分页 / 手动双面拆分
            duplex = settings.get("duplex", "off")
            page_ranges = str(settings.get("page_ranges") or "")
            pass1_path = print_pdf
            pass2_path: Path | None = None
            pass1_pages = total_pages
            pass2_pages = 0

            if duplex != "off":
                first, second = docs.split_pages(page_ranges, total_pages)
                pass1_path = work / "pass1.pdf"
                docs.extract_pages(print_pdf, [page - 1 for page in first], pass1_path)
                pass1_pages = len(first)
                if second:
                    pass2_path = work / "pass2.pdf"
                    docs.extract_pages(
                        print_pdf, [page - 1 for page in second], pass2_path,
                        reverse=bool(settings.get("reverse_second_pass", True)),
                    )
                    pass2_pages = len(second)
            elif page_ranges:
                selected = docs.parse_page_ranges(page_ranges, total_pages)
                if len(selected) != total_pages:
                    trimmed = work / "print.pdf"
                    docs.extract_pages(print_pdf, [page - 1 for page in selected], trimmed)
                    pass1_path = trimmed
                    pass1_pages = len(selected)

            # 3) 投递第一遍
            resolved = ppd.build_lp_options(settings, catalog, roles)
            title = self._job_title(job, duplex, second_pass=False)
            result = printer.submit(pass1_path, resolved.args, title, int(settings.get("copies", 1)))

            warnings = list(resolved.warnings)
            printed_pages = pass1_pages + pass2_pages if duplex != "off" else pass1_pages
            extra = {
                "pages": printed_pages,
                "pass1_path": str(pass1_path),
                "pass2_path": str(pass2_path) if pass2_path else "",
                "pass1_pages": pass1_pages,
                "pass2_pages": pass2_pages,
                "cups_id1": result.job_id,
                "command1": result.command,
                "backend": backend.kind,
                "printer": backend.queue or printer.queue,
            }
            message = result.message or "已送打印"
            if warnings:
                message += " ｜ " + " ".join(warnings)

            if duplex != "off" and pass2_path is not None:
                self._set_state(
                    job_id, STATE_AWAITING_FLIP,
                    message=duplex_hint(settings),
                    expect=(STATE_PREPARING, STATE_PRINTING),
                    extra=extra,
                )
            else:
                note = "第一面已送打印；只有 1 页，无需第二面。" if duplex != "off" else message
                self._set_state(
                    job_id, STATE_DONE,
                    message=note if duplex != "off" else message,
                    expect=(STATE_PREPARING, STATE_PRINTING),
                    extra=extra,
                )
        except (docs.DocumentError, PrintError, printers.BackendError,
                settings_mod.SettingsError) as exc:
            self._fail(job_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._fail(job_id, f"打印失败：{exc}")

    # ------------------------------------------------------------------ 第二遍
    def _run_second(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job or job["state"] != STATE_PRINTING_SECOND:
            return
        try:
            settings = json.loads(job["settings_json"] or "{}")
            backend = printers.select_backend(self.cfg, preferred_queue=self._preferred_queue())
            catalog = ppd.load_catalog(self.cfg, backend.queue or str(self.cfg.get("printer.name")))
            roles = ppd.resolve_roles(catalog)
            printer = printers.printer_for(self.cfg, backend)

            pass2_path = Path(job["pass2_path"] or "")
            if not pass2_path.is_file():
                raise docs.DocumentError("第二面文件已丢失，请重新提交任务")

            resolved = ppd.build_lp_options(settings, catalog, roles)
            title = self._job_title(job, settings.get("duplex", "off"), second_pass=True)
            result = printer.submit(pass2_path, resolved.args, title, int(settings.get("copies", 1)))

            message = "双面打印完成。" + (result.message or "")
            if resolved.warnings:
                message += " ｜ " + " ".join(resolved.warnings)
            self._set_state(
                job_id, STATE_DONE,
                message=message,
                expect=(STATE_PRINTING_SECOND,),
                extra={"cups_id2": result.job_id, "command2": result.command},
            )
        except (docs.DocumentError, PrintError, printers.BackendError,
                settings_mod.SettingsError) as exc:
            self._fail(job_id, f"第二面打印失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._fail(job_id, f"第二面打印失败：{exc}")

    # ------------------------------------------------------------------ 照片
    def _prepare_photo(self, job: dict[str, Any], source: Path, settings: dict[str, Any],
                       work: Path) -> Path:
        photo = settings.get("photo") or {}
        from . import media as media_mod

        media_obj = media_mod.find(str(photo.get("media") or settings.get("media")))
        if media_obj is None:
            media_obj = media_mod.find("4x6in") or media_mod.MEDIA[0]
        dpi = int(photo.get("dpi", 300) or 300)
        fit = str(photo.get("fit", "fit") or "fit")
        border = float(photo.get("border_mm", 3.0) or 0)
        rotate = bool(photo.get("auto_rotate", True))

        target = work / "photo.pdf"
        kind = job["kind"]

        if kind == docs.KIND_IMAGE:
            docs.compose_photo(source, target, media_obj, dpi, fit, border, rotate)
            return target

        # PDF 当照片打：逐页渲染后排版
        if kind == docs.KIND_PDF:
            images = docs.render_pdf_pages(source, work / "pages", self.cfg, dpi)
            docs.photo_pdf_from_images(images, target, media_obj, dpi, fit, border, rotate)
            return target

        # Word 也能打照片模式：先转 PDF 再渲染
        pdf = docs.convert_word(source, work, self.cfg, profile_dir=self.cfg.soffice_profile_dir / job["id"])
        images = docs.render_pdf_pages(pdf, work / "pages", self.cfg, dpi)
        docs.photo_pdf_from_images(images, target, media_obj, dpi, fit, border, rotate)
        return target

    # ------------------------------------------------------------------ 用户操作
    def continue_job(self, job_id: str, actor: str = "") -> dict[str, Any]:
        """「继续打印」：原子地把 awaiting_flip 推进到 printing_second。"""
        with transaction(self.db.conn):
            row = query_one(self.db.conn, "SELECT * FROM jobs WHERE id = ?", (job_id,))
            if not row:
                raise JobError("任务不存在", 404)
            state = row["state"]
            if state != STATE_AWAITING_FLIP:
                raise JobError(
                    f"当前任务状态是「{STATE_LABELS.get(state, state)}」，不能继续打印第二面", 409
                )
            cursor = execute(
                self.db.conn,
                "UPDATE jobs SET state = ?, message = ?, updated_at = ?, continued_at = ? "
                "WHERE id = ? AND state = ?",
                (STATE_PRINTING_SECOND, "收到确认，正在打印第二面…", utcnow(), utcnow(),
                 job_id, STATE_AWAITING_FLIP),
            )
            if cursor.rowcount != 1:
                raise JobError("任务状态已变化，请刷新页面", 409)
        self.queue.put(("second", job_id))
        job = self.get(job_id) or {}
        return job

    def cancel(self, job_id: str, actor: str = "") -> dict[str, Any]:
        job = self.get(job_id)
        if not job:
            raise JobError("任务不存在", 404)
        if job["state"] in {STATE_DONE, STATE_CANCELED}:
            raise JobError("任务已结束，无需取消", 409)

        # 去 CUPS 里撤掉已提交的部分
        try:
            backend = printers.select_backend(self.cfg, preferred_queue=self._preferred_queue())
            printer = printers.printer_for(self.cfg, backend)
            for key in ("cups_id1", "cups_id2"):
                if job.get(key):
                    printer.cancel(str(job[key]))
        except Exception:  # noqa: BLE001
            pass

        self._set_state(
            job_id, STATE_CANCELED,
            message="已取消" if job["state"] != STATE_AWAITING_FLIP else "已取消（第二面不会再打印）",
            expect=(STATE_QUEUED, STATE_PREPARING, STATE_PRINTING, STATE_AWAITING_FLIP,
                    STATE_PRINTING_SECOND),
        )
        return self.get(job_id) or {}

    def retry(self, job_id: str, actor: str = "") -> dict[str, Any]:
        job = self.get(job_id)
        if not job:
            raise JobError("任务不存在", 404)
        if job["state"] not in {STATE_FAILED, STATE_CANCELED}:
            raise JobError("只有失败或已取消的任务可以重试", 409)
        execute(
            self.db.conn,
            "UPDATE jobs SET state=?, message=?, error='', cups_id1='', cups_id2='', "
            "command1='', command2='', continued_at=NULL, finished_at=NULL, updated_at=? WHERE id=?",
            (STATE_QUEUED, "已重新排队", utcnow(), job_id),
        )
        self.queue.put(("first", job_id))
        return self.get(job_id) or {}

    # ------------------------------------------------------------------ 展示
    def _job_title(self, job: dict[str, Any], duplex: str, second_pass: bool) -> str:
        base = job.get("title") or job.get("source_name") or "打印任务"
        suffix = ""
        if duplex != "off":
            suffix = "（第2面 偶数页）" if second_pass else "（第1面 奇数页）"
        return f"{base}{suffix}"[:120]

    def public(self, job: dict[str, Any]) -> dict[str, Any]:
        """转成前端结构，附带可执行动作。"""
        if not job:
            return {}
        try:
            settings = json.loads(job.get("settings_json") or "{}")
        except json.JSONDecodeError:
            settings = {}
        state = str(job.get("state") or "")
        return {
            "id": job.get("id"),
            "title": job.get("title") or job.get("source_name"),
            "owner": job.get("owner_name"),
            "kind": job.get("kind"),
            "source_name": job.get("source_name"),
            "source_bytes": job.get("source_bytes", 0),
            "pages": job.get("pages", 0),
            "pass1_pages": job.get("pass1_pages", 0),
            "pass2_pages": job.get("pass2_pages", 0),
            "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "message": job.get("message", ""),
            "error": job.get("error", ""),
            "backend": job.get("backend", ""),
            "printer": job.get("printer", ""),
            "virtual": job.get("backend") == "virtual",
            "cups_id1": job.get("cups_id1", ""),
            "cups_id2": job.get("cups_id2", ""),
            "command1": job.get("command1", ""),
            "command2": job.get("command2", ""),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "continued_at": job.get("continued_at"),
            "settings": settings,
            "settings_text": settings_mod.describe(settings) if settings else "",
            "duplex": settings.get("duplex", "off"),
            "can_continue": state == STATE_AWAITING_FLIP,
            "can_cancel": state in {STATE_QUEUED, STATE_PREPARING, STATE_PRINTING,
                                    STATE_AWAITING_FLIP, STATE_PRINTING_SECOND},
            "can_retry": state in {STATE_FAILED, STATE_CANCELED},
            "busy": state in {STATE_QUEUED, STATE_PREPARING, STATE_PRINTING, STATE_PRINTING_SECOND},
        }
