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


def test_services_pages_render(logged_in):
    assert logged_in.get("/").status_code == 200
    assert logged_in.get("/history").status_code == 200
    assert logged_in.get("/settings").status_code == 200
    assert logged_in.get("/settings/doctor").status_code == 200


def test_bad_login_does_not_leak_user(logged_in):
    response = logged_in.post("/api/auth/login",
                              json={"username": "no-such-user", "password": "x"})
    assert response.status_code == 401
    assert "不存在" not in response.get_json()["error"]
