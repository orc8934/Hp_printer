"""任务状态机：手动双面两遍流程（核心需求）。"""

from __future__ import annotations

import io
import time

from pypdf import PdfReader, PdfWriter

from hp4928.jobs import (
    STATE_AWAITING_FLIP,
    STATE_CANCELED,
    STATE_DONE,
    STATE_QUEUED,
)


def make_pdf(widths=(600, 601, 602, 603, 604)) -> bytes:
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=float(width), height=842.0)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload(client, payload: bytes, name: str = "doc.pdf") -> dict:
    response = client.post(
        "/api/upload",
        data={"file": (io.BytesIO(payload), name)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["upload"]


def submit(client, upload_info: dict, settings: dict | None = None, mode: str = "document"):
    response = client.post(
        "/api/jobs",
        json={
            "upload_id": upload_info["upload_id"],
            "mode": mode,
            "settings": settings or {},
            "title": "测试任务",
        },
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["job"]


def wait_for(client, job_id: str, states: tuple[str, ...], timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.get_json()["job"]
        if job["state"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 在 {timeout}s 内没有进入 {states}，当前 {job['state']}")


def spool_pdfs(app) -> list:
    cfg = app.extensions["hp4928"].cfg
    return sorted(cfg.spool_dir.glob("*.pdf"))


def page_widths(pdf_bytes: bytes) -> list[float]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [round(float(page.mediabox.width), 1) for page in reader.pages]


def test_manual_duplex_full_flow(logged_in, app):
    upload_info = upload(logged_in, make_pdf())
    job = submit(logged_in, upload_info, {"duplex": "long-edge", "color": "mono", "media": "A4"})
    assert job["state"] in {STATE_QUEUED, "preparing", "printing"}

    job = wait_for(logged_in, job["id"], (STATE_AWAITING_FLIP,))
    assert job["pages"] == 5
    assert job["pass1_pages"] == 3
    assert job["pass2_pages"] == 2
    assert job["can_continue"] is True
    assert "继续打印" in job["message"]
    assert "长边" in job["message"]

    # 第一遍只有奇数页
    pass1 = logged_in.get(f"/api/jobs/{job['id']}/file/pass1")
    assert pass1.status_code == 200
    assert page_widths(pass1.data) == [600.0, 602.0, 604.0]

    # 关键：第二遍绝不能自动打印
    assert len(spool_pdfs(app)) == 1
    time.sleep(1.2)
    assert len(spool_pdfs(app)) == 1
    still = logged_in.get(f"/api/jobs/{job['id']}").get_json()["job"]
    assert still["state"] == STATE_AWAITING_FLIP

    # 用户点「继续打印」
    response = logged_in.post(f"/api/jobs/{job['id']}/continue")
    assert response.status_code == 200, response.get_json()
    job = wait_for(logged_in, job["id"], (STATE_DONE,))
    assert job["state"] == STATE_DONE
    assert job["can_continue"] is False
    assert len(spool_pdfs(app)) == 2

    # 第二遍是偶数页逆序 → 4,2
    pass2 = logged_in.get(f"/api/jobs/{job['id']}/file/pass2")
    assert page_widths(pass2.data) == [603.0, 601.0]

    # 重复点击必须被拒绝，避免重复打印
    again = logged_in.post(f"/api/jobs/{job['id']}/continue")
    assert again.status_code == 409
    assert len(spool_pdfs(app)) == 2


def test_duplex_second_pass_order_can_be_forward(logged_in, app):
    upload_info = upload(logged_in, make_pdf((600, 601, 602, 603)))
    job = submit(
        logged_in, upload_info,
        {"duplex": "short-edge", "reverse_second_pass": False},
    )
    job = wait_for(logged_in, job["id"], (STATE_AWAITING_FLIP,))
    assert "短边" in job["message"]
    logged_in.post(f"/api/jobs/{job['id']}/continue")
    wait_for(logged_in, job["id"], (STATE_DONE,))
    pass2 = logged_in.get(f"/api/jobs/{job['id']}/file/pass2")
    assert page_widths(pass2.data) == [601.0, 603.0]


def test_simplex_job_finishes_in_one_pass(logged_in, app):
    upload_info = upload(logged_in, make_pdf((600, 600, 600)))
    job = submit(logged_in, upload_info, {"duplex": "off"})
    job = wait_for(logged_in, job["id"], (STATE_DONE, "failed"))
    assert job["state"] == STATE_DONE
    assert job["pages"] == 3
    assert len(spool_pdfs(app)) == 1


def test_single_page_duplex_needs_no_second_pass(logged_in, app):
    upload_info = upload(logged_in, make_pdf((600,)))
    job = submit(logged_in, upload_info, {"duplex": "long-edge"})
    job = wait_for(logged_in, job["id"], (STATE_DONE, "failed", STATE_AWAITING_FLIP))
    assert job["state"] == STATE_DONE
    assert "无需第二面" in job["message"]


def test_page_ranges_are_applied(logged_in, app):
    upload_info = upload(logged_in, make_pdf((600, 601, 602, 603, 604)))
    job = submit(logged_in, upload_info, {"duplex": "off", "page_ranges": "2-4"})
    job = wait_for(logged_in, job["id"], (STATE_DONE, "failed"))
    assert job["state"] == STATE_DONE
    assert job["pages"] == 3
    pass1 = logged_in.get(f"/api/jobs/{job['id']}/file/pass1")
    assert page_widths(pass1.data) == [601.0, 602.0, 603.0]


def test_cancel_while_awaiting_flip_never_prints_second_pass(logged_in, app):
    upload_info = upload(logged_in, make_pdf((600, 601, 602, 603)))
    job = submit(logged_in, upload_info, {"duplex": "long-edge"})
    job = wait_for(logged_in, job["id"], (STATE_AWAITING_FLIP,))
    before = len(spool_pdfs(app))

    response = logged_in.post(f"/api/jobs/{job['id']}/cancel")
    assert response.status_code == 200
    assert response.get_json()["job"]["state"] == STATE_CANCELED

    again = logged_in.post(f"/api/jobs/{job['id']}/continue")
    assert again.status_code == 409
    time.sleep(0.6)
    assert len(spool_pdfs(app)) == before


def test_retry_after_cancel(logged_in, app):
    upload_info = upload(logged_in, make_pdf((600, 601)))
    job = submit(logged_in, upload_info, {"duplex": "off"})
    wait_for(logged_in, job["id"], (STATE_DONE,))
    response = logged_in.post(f"/api/jobs/{job['id']}/cancel")
    assert response.status_code == 409  # 已完成不能取消

    # 取消一个进行中的任务再重试
    upload_info = upload(logged_in, make_pdf((600, 601, 602)))
    job2 = submit(logged_in, upload_info, {"duplex": "long-edge"})
    wait_for(logged_in, job2["id"], (STATE_AWAITING_FLIP,))
    logged_in.post(f"/api/jobs/{job2['id']}/cancel")
    retry = logged_in.post(f"/api/jobs/{job2['id']}/retry")
    assert retry.status_code == 200
    assert retry.get_json()["job"]["state"] in {STATE_QUEUED, "preparing", "printing"}
    wait_for(logged_in, job2["id"], (STATE_AWAITING_FLIP,))


def test_job_access_control(logged_in, app):
    from hp4928 import auth

    upload_info = upload(logged_in, make_pdf((600, 600)))
    job = submit(logged_in, upload_info, {"duplex": "off"})
    wait_for(logged_in, job["id"], (STATE_DONE, "failed"))

    store = app.extensions["hp4928"]
    auth.create_user(store.conn, "other", "Other-pass-1", role="user")
    other = app.test_client()
    other.post("/api/auth/login", json={"username": "other", "password": "Other-pass-1"})

    assert other.get(f"/api/jobs/{job['id']}").status_code == 403
    assert other.get("/api/jobs").get_json()["jobs"] == []
