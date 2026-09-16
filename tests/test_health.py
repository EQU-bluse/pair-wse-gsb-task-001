from app.routers import health


async def test_liveness(client):
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_ok(client):
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


async def test_readiness_db_down(client, monkeypatch):
    async def broken():
        raise ConnectionError("database is down")

    monkeypatch.setattr(health, "check_db", broken)
    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"
