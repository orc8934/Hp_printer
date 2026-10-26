"""打印机/队列管理：列表、切换、添加网络打印机、连通性测试。"""

from __future__ import annotations

import pytest

from hp4928 import printers
from hp4928.cups import QueueState
from hp4928.proc import RunResult


def _fake_queues():
    return [
        QueueState(name="HP4928", device_uri="usb://HP/DeskJet%204900%20series?serial=X",
                   state_text="printer HP4928 is idle"),
        QueueState(name="HP4928-NET", device_uri="ipp://192.168.1.50/ipp/print",
                   state_text="printer HP4928-NET is idle"),
    ]


@pytest.fixture()
def fake_cups(app, monkeypatch):
    # 这些用例要验证「真机后端如何选队列」，所以后端改成 auto
    app.extensions["hp4928"].cfg._raw["printer"]["backend"] = "auto"
    monkeypatch.setattr(printers, "list_queues", lambda cfg: _fake_queues())
    monkeypatch.setattr(printers, "cups_available", lambda cfg: True)
    monkeypatch.setattr(printers, "scheduler_running", lambda cfg: True)
    monkeypatch.setattr(printers, "usb_printer_present", lambda cfg: (True, "HP DeskJet 4900"))
    monkeypatch.setattr(printers, "can_manage_queues", lambda cfg: True)
    monkeypatch.setattr(printers, "manage_hint", lambda cfg: "")
    # lpadmin/cupsenable 在测试机（Windows）上不存在，统一假造路径
    monkeypatch.setattr(printers, "which", lambda name: "/usr/bin/" + name)
    return monkeypatch


def test_list_printers_shows_usb_and_network(logged_in, fake_cups):
    payload = logged_in.get("/api/printers").get_json()
    assert payload["ok"] is True
    names = [item["name"] for item in payload["queues"]]
    assert names == ["HP4928", "HP4928-NET"]
    uris = {item["name"]: item["device_uri"] for item in payload["queues"]}
    assert uris["HP4928"].startswith("usb://")
    assert uris["HP4928-NET"].startswith("ipp://")


def test_select_network_queue_persists(logged_in, app, fake_cups):
    response = logged_in.post("/api/printers/select", json={"name": "HP4928-NET"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["selected"] == "HP4928-NET"

    store = app.extensions["hp4928"]
    assert store.selected_queue() == "HP4928-NET"
    # 下一次请求仍然生效（存在 DB 里）
    assert logged_in.get("/api/printers").get_json()["selected"] == "HP4928-NET"

    backend = printers.select_backend(store.cfg, preferred_queue=store.selected_queue())
    assert backend.kind == "cups"
    assert backend.queue == "HP4928-NET"
    assert backend.device_uri.startswith("ipp://")


def test_select_unknown_queue_is_rejected(logged_in, fake_cups):
    response = logged_in.post("/api/printers/select", json={"name": "NOPE"})
    assert response.status_code == 404


def test_select_empty_resets_to_config(logged_in, app, fake_cups):
    logged_in.post("/api/printers/select", json={"name": "HP4928-NET"})
    response = logged_in.post("/api/printers/select", json={"name": ""})
    assert response.status_code == 200
    store = app.extensions["hp4928"]
    assert store.selected_queue() == str(store.cfg.get("printer.name"))


def test_add_queue_validation(logged_in, fake_cups):
    bad_name = logged_in.post("/api/printers/add",
                              json={"name": "坏 名字", "uri": "ipp://1.2.3.4/ipp/print"})
    assert bad_name.status_code == 400
    assert "队列名" in bad_name.get_json()["error"]

    bad_uri = logged_in.post("/api/printers/add",
                             json={"name": "OK", "uri": "ftp://1.2.3.4/ipp/print"})
    assert bad_uri.status_code == 400
    assert "网络地址" in bad_uri.get_json()["error"]


def test_add_queue_success(logged_in, app, fake_cups):
    calls = []

    def fake_run(command, timeout=30):
        calls.append(command)
        return RunResult(" ".join(command), 0, "lpadmin: Printer added", "")

    fake_cups.setattr(printers, "_run", fake_run)

    response = logged_in.post(
        "/api/printers/add",
        json={"name": "HP4928-WIFI", "uri": "ipp://192.168.1.60/ipp/print", "select": True},
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["model"] == "everywhere"
    assert payload["selected"] == "HP4928-WIFI"

    lpadmin_calls = [cmd for cmd in calls if cmd[0].endswith("lpadmin")]
    assert lpadmin_calls, calls
    assert "-m" in lpadmin_calls[0]
    assert "everywhere" in lpadmin_calls[0]
    assert "ipp://192.168.1.60/ipp/print" in lpadmin_calls[0]

    store = app.extensions["hp4928"]
    assert store.selected_queue() == "HP4928-WIFI"


def test_add_queue_reports_lpadmin_failure(logged_in, fake_cups):
    fake_cups.setattr(
        printers, "_run",
        lambda command, timeout=30: RunResult(
            " ".join(command), 1, "", "lpadmin: Unable to connect to 192.168.1.60:631"
        ),
    )
    response = logged_in.post("/api/printers/add",
                              json={"name": "DEAD", "uri": "ipp://192.168.1.60/ipp/print"})
    assert response.status_code == 400
    message = response.get_json()["error"]
    assert "添加队列失败" in message
    assert "Unable to connect" in message


def test_add_queue_needs_lpadmin_group(logged_in, fake_cups):
    fake_cups.setattr(printers, "can_manage_queues", lambda cfg: False)
    fake_cups.setattr(printers, "manage_hint", lambda cfg: "当前服务账号不在 lpadmin 组")
    response = logged_in.post("/api/printers/add",
                              json={"name": "X", "uri": "ipp://1.2.3.4/ipp/print"})
    assert response.status_code == 400
    assert "lpadmin" in response.get_json()["error"]


def test_delete_selected_queue_is_refused(logged_in, fake_cups):
    logged_in.post("/api/printers/select", json={"name": "HP4928-NET"})
    response = logged_in.delete("/api/printers/HP4928-NET")
    assert response.status_code == 409
    assert "正在使用" in response.get_json()["error"]


def test_delete_other_queue(logged_in, fake_cups):
    calls = []

    def fake_run(command, timeout=30):
        calls.append(command)
        return RunResult(" ".join(command), 0, "", "")

    fake_cups.setattr(printers, "_run", fake_run)
    logged_in.post("/api/printers/select", json={"name": "HP4928"})
    response = logged_in.delete("/api/printers/HP4928-NET")
    assert response.status_code == 200
    assert any("-x" in cmd for cmd in calls)


def test_discover_lists_network_uris(logged_in, fake_cups):
    def fake_run(command, timeout=30):
        return RunResult(" ".join(command), 0,
                         "direct usb://HP/DeskJet%204900%20series\n"
                         "network ipp://192.168.1.50/ipp/print\n"
                         "network dnssd://HP%20DeskJet._ipp._tcp.local/\n", "")

    fake_cups.setattr(printers, "_run", fake_run)
    # 只让 lpinfo 存在，ippfind 不存在
    fake_cups.setattr(printers, "which",
                      lambda name: "/usr/bin/lpinfo" if name == "lpinfo" else None)
    payload = logged_in.get("/api/printers/discover").get_json()
    uris = [item["uri"] for item in payload["printers"]]
    assert "ipp://192.168.1.50/ipp/print" in uris
    assert all(item["is_network"] for item in payload["printers"])
    assert all("usb://" not in uri for uri in uris)
    assert any("cups-ipp-utils" in note for note in payload["notes"])


def test_discover_via_ippfind(logged_in, fake_cups):
    def fake_run(command, timeout=30):
        if command[0].endswith("ippfind"):
            return RunResult(" ".join(command), 0,
                             "ipp://192.168.1.77:631/ipp/print\n", "")
        return RunResult(" ".join(command), 0, "", "")

    fake_cups.setattr(printers, "_run", fake_run)
    fake_cups.setattr(printers, "which", lambda name: "/usr/bin/" + name)
    fake_cups.setattr("hp4928.api.which", lambda name: "/usr/bin/" + name)
    payload = logged_in.get("/api/printers/discover").get_json()
    uris = [item["uri"] for item in payload["printers"]]
    assert "ipp://192.168.1.77:631/ipp/print" in uris
    assert payload["notes"] == []


def test_printer_management_requires_admin(app, fake_cups):
    from hp4928 import auth

    store = app.extensions["hp4928"]
    auth.create_user(store.conn, "normal2", "Normal-pass-1", role="user")
    client = app.test_client()
    login = client.post("/api/auth/login",
                        json={"username": "normal2", "password": "Normal-pass-1"})
    client.environ_base["HTTP_X_CSRF_TOKEN"] = login.get_json()["csrf"]
    assert client.post("/api/printers/add",
                       json={"name": "X", "uri": "ipp://1.1.1.1/ipp/print"}).status_code == 403
    assert client.get("/api/printers/discover").status_code == 403
    # 只读的队列列表对普通用户开放
    assert client.get("/api/printers").status_code == 200


def test_status_reports_selected_queue(logged_in, app, fake_cups):
    logged_in.post("/api/printers/select", json={"name": "HP4928-NET"})
    payload = logged_in.get("/api/status").get_json()
    assert payload["selected"] == "HP4928-NET"
    assert payload["backend"]["queue"] == "HP4928-NET"
    assert payload["backend"]["device_uri"].startswith("ipp://")
    assert payload["can_manage"] is True


def test_queue_name_validation_helpers():
    assert printers.QUEUE_NAME_RE.match("HP4928-NET")
    assert not printers.QUEUE_NAME_RE.match("bad name")
    assert not printers.QUEUE_NAME_RE.match("a" * 61)
    assert printers._friendly_name("ipp://192.168.1.50/ipp/print") == "192.168.1.50"
    assert printers._scheme_of("dnssd://HP._ipp._tcp.local/") == "dnssd://"
