"""应用状态容器：配置 + 数据库 + 任务队列 + 驱动能力缓存。"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import __version__, auth, ppd, printers
from .config import Config
from .db import Database
from .jobs import JobManager


class Store:
    def __init__(self, cfg: Config, workers: int | None = None) -> None:
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        worker_count = workers if workers is not None else int(cfg.get("server.workers", 2) or 2)
        self.jobs = JobManager(cfg, self.db, workers=worker_count)
        self._catalog_lock = threading.Lock()
        self._catalog_cache: dict[str, tuple[float, ppd.Catalog]] = {}
        self._catalog_ttl = 30.0
        self._bootstrapped = False
        self.started_at = time.time()

    # ------------------------------------------------------------------ 基础
    @property
    def conn(self):
        return self.db.conn

    @property
    def version(self) -> str:
        return __version__

    def bootstrap(self) -> dict[str, Any]:
        """首次运行初始化：目录、数据库、默认管理员。幂等。"""
        if self._bootstrapped:
            return {"created": False}
        self.cfg.ensure_layout()
        self.db.initialize()
        username, password = auth.bootstrap_admin(self.cfg, self.conn)
        self._bootstrapped = True
        return {
            "created": password is not None,
            "username": username,
            "password": password,
            "data_dir": str(self.cfg.data_dir),
            "db": str(self.cfg.db_path),
        }

    def start(self) -> None:
        self.bootstrap()
        self.jobs.start()

    def stop(self) -> None:
        self.jobs.stop()
        self.db.close()

    # ------------------------------------------------------------------ 队列选择
    def selected_queue(self) -> str:
        """界面上选中的队列（存在 DB 里，优先于配置的 printer.name）。"""
        from .db import kv_get

        try:
            stored = kv_get(self.conn, "printer.queue", "")
        except Exception:  # noqa: BLE001 - 读不到就退回配置
            stored = ""
        return stored or str(self.cfg.get("printer.name", "") or "")

    def set_selected_queue(self, name: str) -> str:
        from .db import kv_set

        name = (name or "").strip()
        kv_set(self.conn, "printer.queue", name)
        with self._catalog_lock:
            self._catalog_cache.clear()
        return name

    # ------------------------------------------------------------------ 驱动能力
    def catalog(self, refresh: bool = False, queue: str = "") -> ppd.Catalog:
        """带缓存的能力目录（避免每次请求都 exec lpoptions）。"""
        queue = queue or self.selected_queue()
        key = queue or "-"
        now = time.time()
        with self._catalog_lock:
            cached = self._catalog_cache.get(key)
            if cached and not refresh and now - cached[0] < self._catalog_ttl:
                return cached[1]
        catalog = ppd.load_catalog(self.cfg, queue)
        with self._catalog_lock:
            self._catalog_cache[key] = (now, catalog)
        return catalog

    def roles(self, catalog: ppd.Catalog | None = None) -> dict[str, str]:
        return ppd.resolve_roles(catalog or self.catalog())

    def schema(self, refresh: bool = False) -> dict[str, Any]:
        catalog = self.catalog(refresh=refresh)
        return ppd.build_schema(self.cfg, catalog, self.roles(catalog))

    def status(self, refresh: bool = True) -> dict[str, Any]:
        info = printers.status(self.cfg, preferred_queue=self.selected_queue())
        info["version"] = self.version
        info["port"] = self.cfg.port
        info["urls"] = self.cfg.lan_urls()
        info["uptime_seconds"] = int(time.time() - self.started_at)
        info["jobs"] = self.jobs.stats()
        info["data_dir"] = str(self.cfg.data_dir)
        info["configured_queue"] = str(self.cfg.get("printer.name", "") or "")
        if refresh:
            try:
                catalog = self.catalog()
                info["driver"] = {
                    "source": catalog.source,
                    "description": catalog.description,
                    "path": catalog.path,
                    "options": len(catalog.options),
                }
            except Exception as exc:  # noqa: BLE001
                info["driver"] = {"source": "error", "description": str(exc)}
        return info

    def doctor(self) -> list[dict[str, Any]]:
        return [check.to_public() for check in printers.doctor(self.cfg)]
