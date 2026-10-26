"""命令行入口：serve / init / passwd / doctor / options / self-test 等。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__, auth, docs, ppd, printers, settings as settings_mod
from .config import dump_example_yaml, load_config
from .db import Database
from .store import Store

BANNER = r"""
  _  _ ___  ___ ___  _   ___    ____  ____   ___ _  _
 | || | _ \| _ \ __|/_\ | __|  |  _ \|  _ \ / _ \ || |
 | __ |  _/|  _/ _| / _ \| _|   | |_) | |_) | (_) | || |
 |_||_|_|  |_| |___/_/ \_\___|  |____/|____/ \___/|_||_|
"""


def _load_config(args):
    root = Path(__file__).resolve().parent.parent
    if getattr(args, "config", None):
        os.environ["HP4928_CONFIG"] = str(Path(args.config).expanduser().resolve())
    if getattr(args, "data_dir", None):
        os.environ["HP4928_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    overrides = {}
    if getattr(args, "backend", None):
        overrides.setdefault("printer", {})["backend"] = args.backend
    if getattr(args, "port", None):
        overrides.setdefault("server", {})["port"] = int(args.port)
        # create_app() 会重新加载配置，写进环境变量才能保证 banner / doctor / 监听一致
        os.environ["HP4928_PORT"] = str(int(args.port))
    if getattr(args, "host", None):
        overrides.setdefault("server", {})["host"] = args.host
        os.environ["HP4928_HOST"] = args.host
    cfg = load_config(root, overrides)
    cfg.ensure_layout()
    return cfg


# --------------------------------------------------------------------- serve
def serve_targets(host: str, listen_ipv6: bool) -> list[str]:
    """决定要监听哪些地址。

    waitress 在 AF_INET6 上强制 IPV6_V6ONLY=1，所以一个 socket 覆盖不了两个地址族，
    双栈必须起两个 listener。
    """
    host = (host or "").strip()
    if host in {"", "0.0.0.0"}:
        return ["0.0.0.0"] + (["::"] if listen_ipv6 else [])
    if host in {"::", "[::]"}:
        return ["::"]
    return [host]


def cmd_serve(args) -> int:
    from .web import create_app

    # 先解析一次，把 --config / --data-dir / --backend 写进环境，再交给 create_app
    base_cfg = _load_config(args)
    app = create_app(start_workers=True)
    store: Store = app.extensions["hp4928"]
    cfg = store.cfg
    # --host/--port 已在 _load_config 里并进配置，这里直接读，保证 banner、
    # 状态页、doctor 用的是同一个端口
    host = cfg.host
    port = cfg.port
    _ = base_cfg

    info = store.bootstrap()
    print(BANNER)
    print(f"  HP 4928 打印服务 v{__version__}")
    print(f"  配置：{cfg.describe_sources()}")
    print(f"  数据目录：{cfg.data_dir}")
    print(f"  监听：http://{host}:{port}/")
    if info.get("created"):
        print("\n  ⚠ 首次运行，已生成默认账号：")
        print(f"      用户名：{info['username']}")
        print(f"      密码：  {info['password']}")
        print(f"    凭据文件：{cfg.data_dir / 'initial-admin.txt'}（登录后请改密码并删除）")
    else:
        print(f"  账号：已初始化（忘记密码：python3 -m hp4928.cli passwd --user admin）")
    if host in {"0.0.0.0", "::", "[::]"}:
        print("  访问地址：")
        for url in cfg.lan_urls():
            print(f"    {url}")
    print()

    try:
        if args.dev:
            app.run(host=host, port=port, debug=True, use_reloader=False, threaded=True)
        else:
            # waitress 绑 IPv6 时会强制 IPV6_V6ONLY=1，一个 socket 兼顾不了两个族，
            # 所以「0.0.0.0 + listen_ipv6」要起两个 listener。
            targets = serve_targets(host, cfg.listen_ipv6)

            from waitress import create_server

            workers = max(2, int(cfg.get("server.threads", 8) or 8))
            servers = []
            for target in targets:
                try:
                    server = create_server(
                        app,
                        host=target,
                        port=port,
                        threads=workers,
                        ident="hp4928-print",
                        channel_timeout=120,
                        cleanup_interval=30,
                    )
                except OSError as exc:
                    label = "IPv6" if ":" in target else "IPv4"
                    print(f"  [!] {label} 监听 {target}:{port} 失败：{exc}（跳过）")
                    continue
                servers.append((target, server))
                label = "IPv6" if ":" in target else "IPv4"
                print(f"  监听 {label}：{target}:{port}")

            if not servers:
                raise SystemExit("没有任何地址监听成功，请检查端口是否被占用")

            for _target, extra in servers[1:]:
                threading.Thread(target=extra.run, name="waitress-extra", daemon=True).start()
            servers[0][1].run()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止…")
    finally:
        store.stop()
    return 0


# --------------------------------------------------------------------- init
def cmd_init(args) -> int:
    cfg = _load_config(args)
    db = Database(cfg.db_path)
    conn = db.conn
    username, password = auth.bootstrap_admin(cfg, conn)
    users = auth.count_users(conn)
    print(f"数据目录：{cfg.data_dir}")
    print(f"数据库：  {cfg.db_path}")
    print(f"配置：    {cfg.describe_sources()}")
    print(f"账号数：  {users}")
    if password:
        print("\n已创建默认管理员：")
        print(f"  用户名：{username}")
        print(f"  密码：  {password}")
        print(f"  凭据文件：{cfg.data_dir / 'initial-admin.txt'}")
    else:
        print(f"管理员已存在：{username}（如需重置密码：passwd --user {username}）")
    db.close()
    return 0


# --------------------------------------------------------------------- passwd
def cmd_passwd(args) -> int:
    cfg = _load_config(args)
    db = Database(cfg.db_path)
    conn = db.conn
    username = args.user or str(cfg.get("security.default_admin_user", "admin"))
    user = auth.get_user(conn, username)
    if not user:
        print(f"用户 {username} 不存在。可用用户：")
        for item in auth.list_users(conn):
            print(f"  - {item['username']} ({item['role']})")
        db.close()
        return 1
    password = args.password or auth.generate_password(16, symbols=False)
    try:
        auth.set_password(
            conn, username, password, must_change=bool(args.force_change),
            minimum=int(cfg.get("security.min_password_length", 8) or 8),
        )
    except auth.AuthError as exc:
        print(f"失败：{exc.message}")
        db.close()
        return 1
    auth.audit(conn, "cli", "password.reset", username)
    print(f"已重置 {username} 的密码：{password}")
    if args.force_change:
        print("下次登录后需要修改密码。")
    db.close()
    return 0


# --------------------------------------------------------------------- users
def cmd_users(args) -> int:
    cfg = _load_config(args)
    db = Database(cfg.db_path)
    conn = db.conn
    if args.action == "list":
        for item in auth.list_users(conn):
            flag = "（已停用）" if item["disabled"] else ""
            print(f"{item['username']:<20} {item['role']:<6} 最近登录 {item['last_login_at'] or '—'}{flag}")
        return 0
    if args.action == "add":
        if not args.username:
            print("需要 --username")
            return 1
        password = args.password or auth.generate_password(14, symbols=False)
        try:
            auth.create_user(conn, args.username, password, role=args.role or "user")
        except auth.AuthError as exc:
            print(f"失败：{exc.message}")
            return 1
        print(f"已创建 {args.username}（{args.role or 'user'}），密码：{password}")
        return 0
    if args.action == "delete":
        if not args.username:
            print("需要 --username")
            return 1
        try:
            auth.delete_user(conn, args.username)
        except auth.AuthError as exc:
            print(f"失败：{exc.message}")
            return 1
        print(f"已删除 {args.username}")
        return 0
    print("未知操作")
    return 1


# --------------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    cfg = _load_config(args)
    checks = printers.doctor(cfg)
    if args.json:
        print(json.dumps([check.to_public() for check in checks], ensure_ascii=False, indent=2))
    else:
        icons = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
        for check in checks:
            print(f"{icons.get(check.status, '?')} {check.name}: {check.detail}")
            if check.hint:
                print(f"    → {check.hint}")
    failures = [check for check in checks if check.status == "fail"]
    warnings = [check for check in checks if check.status == "warn"]
    print(f"\n合计：{len(checks)} 项，失败 {len(failures)}，提醒 {len(warnings)}")
    return 0 if not failures else 2


# --------------------------------------------------------------------- status
def cmd_status(args) -> int:
    cfg = _load_config(args)
    info = printers.status(cfg)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    backend = info.get("backend", {})
    print(f"后端：{backend.get('kind')}（{backend.get('reason')}）")
    print(f"队列：{backend.get('queue') or '—'}  设备：{backend.get('device_uri') or '—'}")
    print(f"USB：{'已连接' if info.get('usb', {}).get('present') else '未检测到'} — {info.get('usb', {}).get('detail')}")
    for queue in info.get("queues", []):
        print(f"  · {queue['name']:<16} {queue['device_uri'] or '—'}")
    return 0


# --------------------------------------------------------------------- options
def cmd_options(args) -> int:
    cfg = _load_config(args)
    catalog = ppd.load_catalog(cfg, str(cfg.get("printer.name", "")))
    roles = ppd.resolve_roles(catalog)
    print(f"驱动来源：{catalog.source}  {catalog.description}")
    if catalog.path:
        print(f"PPD：{catalog.path}")
    print(f"选项数：{len(catalog.options)}")
    print("\n语义映射：")
    for role, name in sorted(roles.items()):
        option = catalog.get(name)
        print(f"  {role:<12} → {name:<18} 默认 {option.default if option else '—'}")
    print("\n全部驱动选项：")
    for option in catalog.options.values():
        choices = " ".join(
            (f"*{item.name}" if item.is_default else item.name) for item in option.choices
        )
        print(f"  {option.name:<22} {option.label[:28]:<30} {choices[:90]}")

    settings, _ = settings_mod.normalize_settings({}, cfg, catalog, "document")
    if args.json:
        resolved = ppd.build_lp_options(settings, catalog, roles)
        print(json.dumps(resolved.as_dict(), ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------- 配置导出
def cmd_config_dump(args) -> int:
    cfg = _load_config(args)
    text = dump_example_yaml(cfg)
    target = Path(args.output) if args.output else None
    if target:
        target.write_text(text, encoding="utf-8")
        print(f"已写入 {target}")
    else:
        print(text)
    return 0


# --------------------------------------------------------------------- 自检打印
def cmd_self_test(args) -> int:
    """生成测试页并投递，用来验证整条链路。"""
    from . import media as media_mod

    cfg = _load_config(args)
    work = cfg.data_dir / "self-test"
    work.mkdir(parents=True, exist_ok=True)

    media_obj = media_mod.find(args.media) or media_mod.find("A4") or media_mod.MEDIA[0]
    pages = max(1, int(args.pages))
    pdf = docs.make_selftest_pdf(work / "selftest.pdf", media_obj, pages=pages, dpi=300)
    print(f"已生成测试 PDF：{pdf}（{pages} 页，{media_obj.label}）")

    if args.dry_run:
        print("--dry-run：不投递。")
        return 0

    from .db import kv_get

    preferred = kv_get(Database(cfg.db_path).conn, "printer.queue", "")
    backend = printers.select_backend(cfg, preferred_queue=preferred)
    catalog = ppd.load_catalog(cfg, backend.queue or str(cfg.get("printer.name")))
    settings, warnings = settings_mod.normalize_settings({}, cfg, catalog, "document")
    resolved = ppd.build_lp_options(settings, catalog)
    for warning in warnings + resolved.warnings:
        print(f"提醒：{warning}")

    printer = printers.printer_for(cfg, backend)
    print(f"后端：{backend.kind}  队列：{backend.queue or '—'}")
    result = printer.submit(pdf, resolved.args, "HP4928 self-test", 1)
    print(f"结果：{result.message}")
    if result.command:
        print(f"命令：{result.command}")
    if result.output_path:
        print(f"输出：{result.output_path}")
    return 0


# --------------------------------------------------------------------- 打印机管理
def cmd_printers(args) -> int:
    from .db import kv_get, kv_set

    cfg = _load_config(args)
    db = Database(cfg.db_path)
    conn = db.conn
    action = args.action
    selected = kv_get(conn, "printer.queue", "") or str(cfg.get("printer.name", "") or "")

    if action == "list":
        queues = printers.list_queues(cfg)
        if args.json:
            print(json.dumps(
                {"selected": selected, "can_manage": printers.can_manage_queues(cfg),
                 "queues": [item.to_public() for item in queues]},
                ensure_ascii=False, indent=2))
            return 0
        print(f"CUPS 可用：{'是' if printers.cups_available(cfg) else '否'}；"
              f"可管理队列：{'是' if printers.can_manage_queues(cfg) else '否'}")
        if not queues:
            print("（没有队列）")
        for queue in queues:
            mark = "*" if queue.name == selected else " "
            kind = "网络" if queue.device_uri.lower().startswith(
                ("ipp://", "ipps://", "dnssd://", "socket://", "lpd://")) else "USB "
            print(f" {mark} {queue.name:<24} {kind} {queue.device_uri or '—'}")
        if printers.manage_hint(cfg):
            print(f"\n提示：{printers.manage_hint(cfg)}")
        return 0

    if action == "use":
        name = (args.name or "").strip()
        if name:
            queues = printers.list_queues(cfg)
            if not any(item.name == name for item in queues):
                print(f"CUPS 里没有队列 {name}")
                return 1
        kv_set(conn, "printer.queue", name)
        print(f"已切换：{name or '（恢复配置里的 printer.name）'}")
        return 0

    if action == "add":
        if not args.name or not args.uri:
            print("需要 --name 和 --uri，例如：printers add --name HP4928-NET --uri ipp://192.168.1.50/ipp/print")
            return 1
        try:
            result = printers.add_queue(cfg, args.name, args.uri, args.model or "")
        except printers.BackendError as exc:
            print(f"失败：{exc}")
            return 1
        kv_set(conn, "printer.queue", args.name)
        print(f"{result['message']}（驱动 {result['model']}，已设为默认）")
        return 0

    if action == "delete":
        if not args.name:
            print("需要队列名")
            return 1
        if args.name == selected:
            print("这是当前使用的队列，请先 printers use 别的队列")
            return 1
        try:
            result = printers.delete_queue(cfg, args.name)
        except printers.BackendError as exc:
            print(f"失败：{exc}")
            return 1
        print(result["message"])
        return 0

    if action == "test":
        name = (args.name or selected).strip()
        if not name:
            print("没有可测试的队列")
            return 1
        pdf = docs.make_selftest_pdf(
            cfg.data_dir / "self-test" / f"connect-{int(time.time())}.pdf",
            pages=1,
            note=f"queue: {name}",
        )
        from .cups import CupsPrinter

        try:
            result = CupsPrinter(cfg, name).submit(pdf, [], f"connectivity test {name}", 1)
        except Exception as exc:  # noqa: BLE001
            print(f"失败：{exc}")
            return 1
        print(f"已投递到 {name}：{result.message}")
        print(f"命令：{result.command}")
        return 0

    if action == "discover":
        found = printers.discover_network(cfg)
        if args.json:
            print(json.dumps([item.to_public() for item in found], ensure_ascii=False, indent=2))
            return 0
        if not found:
            print("没有发现局域网打印机。可手动加：printers add --name X --uri ipp://IP/ipp/print")
            return 0
        for item in found:
            print(f"  {item.uri}\n      来源 {item.source}，可添加为：--name NET-{item.name[:30]}")
        return 0

    print("未知动作")
    return 1


# --------------------------------------------------------------------- 清理
def cmd_cleanup(args) -> int:
    cfg = _load_config(args)
    db = Database(cfg.db_path)
    removed = printers.cleanup_old_files(cfg, db.conn)
    print(f"已删除 {removed} 个过期任务文件（保留 {cfg.get('retention.days')} 天）")
    db.close()
    return 0


# --------------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hp4928",
        description="HP 4928 (DeskJet 4920 系列) USB / 局域网打印服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用命令：\n"
            "  python3 -m hp4928.cli serve            启动服务（默认 0.0.0.0:10086）\n"
            "  python3 -m hp4928.cli init             初始化数据库与默认账号\n"
            "  python3 -m hp4928.cli passwd --user admin --generate\n"
            "  python3 -m hp4928.cli doctor           环境自检\n"
            "  python3 -m hp4928.cli options          查看驱动选项与映射\n"
            "  python3 -m hp4928.cli self-test        打一张测试页\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"hp4928 {__version__}")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--data-dir", help="数据目录（覆盖配置）")
    parser.add_argument("--backend", choices=["auto", "cups", "virtual"], help="打印后端")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（部分命令）")

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 Web 服务")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--dev", action="store_true", help="用 Flask 开发服务器（仅调试）")
    serve.set_defaults(func=cmd_serve)

    init = sub.add_parser("init", help="初始化数据目录/数据库/默认账号")
    init.set_defaults(func=cmd_init)

    passwd = sub.add_parser("passwd", help="重置账号密码")
    passwd.add_argument("--user", default="admin")
    passwd.add_argument("--password", help="直接指定新密码（不指定则随机生成）")
    passwd.add_argument("--generate", action="store_true", help="随机生成密码（默认行为）")
    passwd.add_argument("--force-change", action="store_true", help="要求下次登录后修改")
    passwd.set_defaults(func=cmd_passwd)

    users = sub.add_parser("users", help="账号管理")
    users.add_argument("action", choices=["list", "add", "delete"])
    users.add_argument("--username")
    users.add_argument("--password")
    users.add_argument("--role", choices=["admin", "user"])
    users.set_defaults(func=cmd_users)

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.set_defaults(func=cmd_doctor)

    status = sub.add_parser("status", help="打印机/队列状态")
    status.set_defaults(func=cmd_status)

    options = sub.add_parser("options", help="查看驱动选项与语义映射")
    options.set_defaults(func=cmd_options)

    printers_cmd = sub.add_parser("printers", help="打印机/队列管理（列表、切换、添加网络打印机）")
    printers_cmd.add_argument("action",
                              choices=["list", "use", "add", "delete", "test", "discover"])
    printers_cmd.add_argument("name", nargs="?", help="队列名")
    printers_cmd.add_argument("--uri", help="打印机地址，如 ipp://192.168.1.50/ipp/print")
    printers_cmd.add_argument("--model", help="驱动/PPD，留空=everywhere")
    printers_cmd.set_defaults(func=cmd_printers)

    config_dump = sub.add_parser("config-dump", help="导出示例配置")
    config_dump.add_argument("--output")
    config_dump.set_defaults(func=cmd_config_dump)

    self_test = sub.add_parser("self-test", help="打印自检页")
    self_test.add_argument("--dry-run", action="store_true")
    self_test.add_argument("--pages", type=int, default=1)
    self_test.add_argument("--media", default="A4")
    self_test.set_defaults(func=cmd_self_test)

    cleanup = sub.add_parser("cleanup", help="清理过期任务文件")
    cleanup.set_defaults(func=cmd_cleanup)

    return parser


_GLOBAL_VALUE_FLAGS = {"--config", "--data-dir", "--backend"}
_GLOBAL_BOOL_FLAGS = {"--json"}


def hoist_globals(argv: list[str]) -> list[str]:
    """允许全局参数写在子命令后面：serve --port 10086 --data-dir x。"""
    globals_: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        name = token.split("=", 1)[0]
        if name in _GLOBAL_VALUE_FLAGS:
            if "=" in token:
                globals_.append(token)
                index += 1
                continue
            if index + 1 < len(argv):
                globals_.extend([token, argv[index + 1]])
                index += 2
                continue
        if token in _GLOBAL_BOOL_FLAGS:
            globals_.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    return globals_ + rest


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(hoist_globals(raw))
    if not getattr(args, "command", None):
        # 默认启动服务，方便 `python3 run.py`
        args = parser.parse_args(hoist_globals(["serve"] + raw))
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr)
        if os.environ.get("HP4928_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
