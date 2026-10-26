"""账号、CSRF、限速、改密。"""

from __future__ import annotations

from conftest import ADMIN_PASSWORD


def test_index_requires_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_api_requires_login(client):
    response = client.get("/api/jobs")
    assert response.status_code == 401
    assert response.get_json()["code"] == "auth_required"


def test_login_wrong_password(client):
    response = client.post("/api/auth/login", json={"username": "tester", "password": "nope"})
    assert response.status_code == 401
    assert "不正确" in response.get_json()["error"]


def test_login_success_and_me(client):
    response = client.post(
        "/api/auth/login", json={"username": "tester", "password": ADMIN_PASSWORD}
    )
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["user"]["is_admin"] is True
    assert payload["csrf"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.get_json()["user"]["username"] == "tester"


def test_csrf_required(logged_in):
    # 故意去掉 CSRF 头
    logged_in.environ_base.pop("HTTP_X_CSRF_TOKEN", None)
    response = logged_in.post("/api/jobs", json={"upload_id": "x"})
    assert response.status_code == 400
    assert response.get_json()["code"] == "csrf"


def test_change_password(logged_in):
    response = logged_in.post(
        "/api/auth/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "New-pass-456"},
    )
    assert response.status_code == 200

    logged_in.post("/api/auth/logout")
    bad = logged_in.post(
        "/api/auth/login", json={"username": "tester", "password": ADMIN_PASSWORD}
    )
    assert bad.status_code == 401
    good = logged_in.post(
        "/api/auth/login", json={"username": "tester", "password": "New-pass-456"}
    )
    assert good.status_code == 200


def test_password_too_short(logged_in):
    response = logged_in.post(
        "/api/auth/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "abc"},
    )
    assert response.status_code == 400


def test_login_rate_limit(app, client):
    store = app.extensions["hp4928"]
    store.cfg._raw["security"]["login_max_attempts"] = 3
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "tester", "password": "wrong"})
    response = client.post("/api/auth/login", json={"username": "tester", "password": "wrong"})
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_logout(client):
    login = client.post("/api/auth/login", json={"username": "tester", "password": ADMIN_PASSWORD})
    client.environ_base["HTTP_X_CSRF_TOKEN"] = login.get_json()["csrf"]
    assert client.get("/api/auth/me").status_code == 200
    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "登录" in response.get_data(as_text=True)
