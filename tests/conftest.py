"""pytest 公共夹具：临时数据目录 + 虚拟打印机 + 已登录客户端。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hp4928 import auth  # noqa: E402
from hp4928.web import create_app  # noqa: E402

ADMIN_PASSWORD = "Test-pass-123"


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        overrides={
            "paths": {"data_dir": str(tmp_path / "data")},
            "printer": {"backend": "virtual", "name": "HP4928"},
            "security": {"login_max_attempts": 50, "min_password_length": 8},
            "server": {"port": 10086, "host": "127.0.0.1"},
        },
        start_workers=True,
    )
    application.config.update(TESTING=True)
    store = application.extensions["hp4928"]
    auth.create_user(store.conn, "tester", ADMIN_PASSWORD, role="admin")
    yield application
    store.stop()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def logged_in(client):
    response = client.post(
        "/api/auth/login", json={"username": "tester", "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    token = response.get_json()["csrf"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token
    return client


@pytest.fixture()
def csrf(logged_in):
    return logged_in.environ_base["HTTP_X_CSRF_TOKEN"]
