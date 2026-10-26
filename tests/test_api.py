"""API 行为：上传校验、设置校验、状态与能力接口。"""

from __future__ import annotations

import io

from conftest import ADMIN_PASSWORD


def make_pdf_bytes(pages: int = 2) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_status_endpoint(logged_in):
    response = logged_in.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["backend"]["kind"] == "virtual"
    assert payload["backend"]["real_printer"] is False
    assert payload["port"] == 10086
    assert payload["version"]


def test_schema_endpoint(logged_in):
    payload = logged_in.get("/api/schema").get_json()
    schema = payload["schema"]
    assert schema["driver"]["source"] in {"builtin", "ppd", "lpoptions"}
    assert schema["capabilities"]["manual_duplex"] is True
    keys = [field["key"] for group in schema["groups"] for field in group["fields"]]
    assert "duplex" in keys
    assert "media" in keys
    photo_keys = [field["key"] for group in schema["groups"] for field in group["fields"]
                  if group["id"] == "photo"]
    assert "photo.media" in photo_keys


def test_media_endpoint(logged_in):
    payload = logged_in.get("/api/media").get_json()
    ids = [item["id"] for item in payload["media"]]
    assert "A4" in ids
    assert "4x6in" in ids


def test_upload_rejects_unknown_type(logged_in):
    response = logged_in.post(
        "/api/upload",
        data={"file": (io.BytesIO(b"not a document" * 10), "evil.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 415
    assert "不支持" in response.get_json()["error"]


def test_upload_requires_login(client):
    response = client.post(
        "/api/upload",
        data={"file": (io.BytesIO(make_pdf_bytes()), "a.pdf")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 401


def test_upload_pdf_metadata(logged_in):
    response = logged_in.post(
        "/api/upload",
        data={"file": (io.BytesIO(make_pdf_bytes(3)), "文档.pdf")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    upload = response.get_json()["upload"]
    assert upload["kind"] == "pdf"
    assert upload["pages"] == 3
    assert upload["name"] == "文档.pdf"
    assert upload["mode_suggestion"] == "document"


def test_upload_image_suggests_photo_mode(logged_in):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (900, 600), "white").save(buffer, "JPEG")
    buffer.seek(0)
    response = logged_in.post(
        "/api/upload",
        data={"file": (buffer, "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    upload = response.get_json()["upload"]
    assert upload["kind"] == "image"
    assert upload["mode_suggestion"] == "photo"


def test_create_job_requires_upload(logged_in):
    response = logged_in.post("/api/jobs", json={"upload_id": "0" * 32, "settings": {}})
    assert response.status_code == 400
    assert "不存在" in response.get_json()["error"]


def test_create_job_rejects_duplex_with_nup(logged_in):
    upload = logged_in.post(
        "/api/upload",
        data={"file": (io.BytesIO(make_pdf_bytes()), "a.pdf")},
        content_type="multipart/form-data",
    ).get_json()["upload"]
    response = logged_in.post(
        "/api/jobs",
        json={
            "upload_id": upload["upload_id"],
            "settings": {"duplex": "long-edge", "pages_per_sheet": 4},
        },
    )
    assert response.status_code == 400
    assert "每张多页" in response.get_json()["error"]


def test_create_job_rejects_bad_page_range(logged_in):
    upload = logged_in.post(
        "/api/upload",
        data={"file": (io.BytesIO(make_pdf_bytes(2)), "a.pdf")},
        content_type="multipart/form-data",
    ).get_json()["upload"]
    response = logged_in.post(
        "/api/jobs",
        json={"upload_id": upload["upload_id"], "settings": {"page_ranges": "9-12"}},
    )
    assert response.status_code == 400
    assert "超出范围" in response.get_json()["error"]


def test_photo_mode_defaults(logged_in):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (600, 900), (10, 40, 200)).save(buffer, "JPEG")
    buffer.seek(0)
    upload = logged_in.post(
        "/api/upload",
        data={"file": (buffer, "photo.jpg")},
        content_type="multipart/form-data",
    ).get_json()["upload"]

    response = logged_in.post(
        "/api/jobs",
        json={"upload_id": upload["upload_id"], "mode": "photo", "settings": {}},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["settings"]["media"] == "4x6in"
    assert body["settings"]["media_type"] == "photo"
    assert body["settings"]["quality"] == "best"


def test_users_api_requires_admin_app(app):
    from hp4928 import auth

    store = app.extensions["hp4928"]
    auth.create_user(store.conn, "normal", "Normal-pass-1", role="user")
    client = app.test_client()
    client.post("/api/auth/login", json={"username": "normal", "password": "Normal-pass-1"})
    assert client.get("/api/users").status_code == 403


def test_healthz_open(client):
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_admin_password_reset_via_api(logged_in):
    response = logged_in.post("/api/users", json={"username": "worker1", "role": "user",
                                                  "generate": True})
    assert response.status_code == 200
    password = response.get_json()["password"]
    assert len(password) >= 8

    login = logged_in.post("/api/auth/login", json={"username": "worker1", "password": password})
    assert login.status_code == 200


def test_normal_user_cannot_see_others_jobs_in_pages(app, logged_in):
    """回归：首页/历史页的 bootstrap 必须按 owner 过滤，不能把别人的任务带出来。"""
    import time as _time

    from hp4928 import auth

    upload = logged_in.post(
        "/api/upload",
        data={"file": (io.BytesIO(make_pdf_bytes(4)), "admin-only.pdf")},
        content_type="multipart/form-data",
    ).get_json()["upload"]
    job = logged_in.post(
        "/api/jobs",
        json={"upload_id": upload["upload_id"], "settings": {"duplex": "long-edge"}},
    ).get_json()["job"]

    deadline = _time.time() + 20
    while _time.time() < deadline:
        state = logged_in.get(f"/api/jobs/{job['id']}").get_json()["job"]["state"]
        if state in {"awaiting_flip", "failed", "done"}:
            break
        _time.sleep(0.05)

    store = app.extensions["hp4928"]
    auth.create_user(store.conn, "viewer2", "Viewer-pass-2", role="user")
    client = app.test_client()
    client.post("/api/auth/login", json={"username": "viewer2", "password": "Viewer-pass-2"})

    for path in ("/", "/history"):
        html = client.get(path).get_data(as_text=True)
        assert "admin-only.pdf" not in html, f"{path} 泄露了别的用户的文件名"
        assert job["id"] not in html, f"{path} 泄露了别的用户的任务 ID"

    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_services_pages_render(logged_in):
    assert logged_in.get("/").status_code == 200
    assert logged_in.get("/history").status_code == 200
    assert logged_in.get("/settings").status_code == 200
    assert logged_in.get("/settings/doctor").status_code == 200
    assert logged_in.get("/account").status_code == 200
    assert logged_in.get("/notifications").status_code == 200
    assert logged_in.get("/users").status_code == 200


def test_new_pages_require_login(client):
    for path in ("/account", "/notifications", "/users"):
        response = client.get(path)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"]


def test_shell_renders_sidebar_and_title(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    assert "hp-sidebar" in html
    assert "当前在：工作台" in html
    assert "系统管理" in html          # 管理员能看到
    assert "用户权限" in html
    assert "hp-nav-item is-active" in html


def test_admin_menu_hidden_for_normal_user(app, logged_in):
    from hp4928 import auth

    store = app.extensions["hp4928"]
    auth.create_user(store.conn, "plain1", "Plain-pass-1", role="user")
    client = app.test_client()
    client.post("/api/auth/login", json={"username": "plain1", "password": "Plain-pass-1"})

    html = client.get("/").get_data(as_text=True)
    assert "当前在：工作台" in html
    assert "个人中心" in html and "消息通知" in html
    assert "系统管理" not in html
    assert "用户权限" not in html

    # 普通用户访问管理员页面被挡回首页
    assert client.get("/settings").status_code == 302
    assert client.get("/users").status_code == 302


def test_settings_no_longer_hosts_user_management(logged_in):
    html = logged_in.get("/settings").get_data(as_text=True)
    assert "打印机与连接" in html
    assert "new-user-form" not in html
    users_html = logged_in.get("/users").get_data(as_text=True)
    assert "new-user-form" in users_html
    assert "plain1" not in users_html


def test_account_page_shows_profile(logged_in):
    html = logged_in.get("/account").get_data(as_text=True)
    assert "修改密码" in html
    assert "password-form" in html
    assert "tester" in html


def test_notifications_lists_duplex_job(logged_in):
    upload = logged_in.post(
        "/api/upload",
        data={"file": (io.BytesIO(make_pdf_bytes(5)), "duplex.pdf")},
        content_type="multipart/form-data",
    ).get_json()["upload"]
    job = logged_in.post(
        "/api/jobs",
        json={"upload_id": upload["upload_id"], "settings": {"duplex": "long-edge"}},
    ).get_json()["job"]

    import time as _time

    deadline = _time.time() + 20
    state = ""
    while _time.time() < deadline:
        state = logged_in.get(f"/api/jobs/{job['id']}").get_json()["job"]["state"]
        if state in {"awaiting_flip", "failed", "done"}:
            break
        _time.sleep(0.05)
    assert state == "awaiting_flip"

    html = logged_in.get("/notifications").get_data(as_text=True)
    assert "需要翻面确认" in html
    assert job["id"] in html
    assert 'data-continue="' + job["id"] + '"' in html



def test_bad_login_does_not_leak_user(logged_in):
    response = logged_in.post("/api/auth/login",
                              json={"username": "no-such-user", "password": "x"})
    assert response.status_code == 401
    assert "不存在" not in response.get_json()["error"]
