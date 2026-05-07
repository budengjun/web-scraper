import dashboard
import time


def test_resume_upload_and_keyword_location_apis(tmp_path):
    dashboard.DB_PATH = str(tmp_path / "app.db")
    dashboard.app.config["TESTING"] = True

    client = dashboard.app.test_client()
    upload = client.post(
        "/api/resumes/upload",
        data={
            "resume": (
                __import__("io").BytesIO(
                    b"Yaolong Hu\nPython React SQL Machine Learning\nCPSC 304 CPSC 330"
                ),
                "resume.txt",
            )
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 200
    payload = upload.get_json()
    assert "Python" in payload["profile"]["skills"]

    keyword_res = client.post("/api/keywords", json={"keyword": "backend developer intern"})
    assert keyword_res.status_code == 200
    assert any(item["keyword"] == "backend developer intern" for item in keyword_res.get_json())

    location_res = client.post("/api/locations", json={"city": "Vancouver", "province_state": "BC"})
    assert location_res.status_code == 200
    assert location_res.get_json()[0]["city"] == "Vancouver"

    preview = client.post("/api/search/preview", json={"platforms": ["Indeed"]})
    assert preview.status_code == 200
    assert preview.get_json()["task_count"] >= 1


def test_search_run_returns_background_task_status(tmp_path):
    dashboard.DB_PATH = str(tmp_path / "empty.db")
    dashboard.app.config["TESTING"] = True

    client = dashboard.app.test_client()
    started = client.post("/api/search/run", json={"platforms": ["Indeed"]})

    assert started.status_code == 202
    task_id = started.get_json()["task_id"]

    status = None
    for _ in range(20):
        status = client.get(f"/api/search/status/{task_id}").get_json()
        if status["status"] in {"failed", "completed"}:
            break
        time.sleep(0.05)

    assert status["status"] == "failed"
    assert "Upload and parse a resume" in status["message"]
