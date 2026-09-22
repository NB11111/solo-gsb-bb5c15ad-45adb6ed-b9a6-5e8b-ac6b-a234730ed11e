"""FastAPI 接口冒烟测试（使用内存 SQLite）。"""

from __future__ import annotations

import os
import time

import pytest

os.environ["OCR_DB_PATH"] = "/tmp/ocr_layout_test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _payload():
    # 跨栏标题 + 左右两栏各 2 行
    words = [
        {"id": "t0", "text": "Title", "confidence": 0.99,
         "quad": [[300, 40], [400, 40], [400, 64], [300, 64]]},
    ]
    for col, cc in enumerate((120, 460)):
        for li in range(2):
            for k in range(2):
                words.append({
                    "id": f"c{col}-{li}-{k}",
                    "text": f"w{col}{li}{k}",
                    "confidence": 0.9,
                    "quad": [
                        [cc - 50 + k * 60, 120 + li * 50 - 10],
                        [cc + 10 + k * 60, 120 + li * 50 - 10],
                        [cc + 10 + k * 60, 120 + li * 50 + 10],
                        [cc - 50 + k * 60, 120 + li * 50 + 10],
                    ],
                })
    return {"page_width": 595, "page_height": 842, "words": words}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_validation_error(client):
    r = client.post("/tasks", json={"page_width": -1, "page_height": 10, "words": []})
    assert r.status_code == 422
    r = client.post("/tasks", json={
        "page_width": 100, "page_height": 100,
        "words": [{"id": 1, "text": "a", "confidence": 2,
                   "quad": [[0, 0], [1, 0], [1, 1]]}],
    })
    assert r.status_code == 422


def test_task_lifecycle(client):
    r = client.post("/tasks", json=_payload())
    assert r.status_code == 201
    tid = r.json()["task_id"]

    for _ in range(50):
        got = client.get(f"/tasks/{tid}").json()
        if got["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert got["status"] == "done", got
    res = got["result"]
    assert res["line_count"] == 5            # 1 标题 + 4 栏内行
    assert len(res["columns"]) == 2
    order = [(b["type"], b.get("column")) for b in res["reading_order"]]
    assert order[0][0] == "spanning"
    assert [o for o in order if o[0] == "column"] == [("column", 0), ("column", 1)]
    assert res["ordered_text"].splitlines()[0] == "Title"


def test_missing_task_404(client):
    assert client.get("/tasks/does-not-exist").status_code == 404
