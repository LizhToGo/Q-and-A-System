def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_validate_returns_traceable_checks(client):
    response = client.post("/api/answers/validate", json={"question": "采样频率为8 kHz时最高可表示频率是多少？", "answer": "fmax = fs / 2，结果为8 kHz，频率6000 Hz。"})
    assert response.status_code == 200
    body = response.json()
    statuses = {item["category"]: item["status"] for item in body["checks"]}
    assert statuses["formula"] == "pass"
    assert statuses["numeric"] == "fail"
    assert statuses["cross_modal"] == "conflict"
    assert body["traceability"]["status"] == "traced"
    assert body["risk_flags"]


def test_unknown_material_id_is_rejected(client):
    response = client.post("/api/answers/validate", json={"question": "问题", "answer": "回答", "material_ids": [999999]})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_material_ids"


def test_metrics_have_case_coverage(client):
    response = client.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["course_count"] >= 3
    assert body["topic_count"] >= 20
    assert body["evaluation_case_count"] >= 100
