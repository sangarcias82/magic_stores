def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "app" in body


def test_home_empty_ok(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "No stores yet" in response.text
    assert "tubos" not in response.text.lower()
    assert "ropa-demo" not in response.text
