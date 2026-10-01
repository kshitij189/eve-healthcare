from redis import Redis

from app.core.redis_client import get_redis


def catalogue_keys() -> list[str]:
    return [k for k in get_redis().keys("catalogue:v*")]


def test_catalogue_reads_are_cached(client, catalogue, monkeypatch):
    first = client.get(f"/centres/{catalogue.alpha.id}")
    assert catalogue_keys() == [f"catalogue:v0:centre:{catalogue.alpha.id}"]

    # A cache hit must not touch the database at all.
    def no_db(*args, **kwargs):
        raise AssertionError("database was queried on a cache hit")

    monkeypatch.setattr("sqlalchemy.orm.Session.scalar", no_db)
    second = client.get(f"/centres/{catalogue.alpha.id}")

    assert second.status_code == 200
    assert second.json() == first.json()


def test_admin_write_invalidates_cached_catalogue(client, catalogue, admin_headers):
    path = f"/centres/{catalogue.alpha.id}/tests"
    before = {t["name"]: t["price"] for t in client.get(path).json()}

    client.patch(f"{path}/{catalogue.cbc.id}", json={"price": "525.00"}, headers=admin_headers)

    after = {t["name"]: t["price"] for t in client.get(path).json()}
    assert before["Complete Blood Count"] == "500.00"
    assert after["Complete Blood Count"] == "525.00"


def test_not_found_is_not_cached(client):
    client.get("/tests/00000000-0000-0000-0000-000000000000")

    assert catalogue_keys() == []


def test_catalogue_still_served_when_redis_is_down(client, catalogue, monkeypatch):
    broken = Redis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2)
    monkeypatch.setattr("app.core.cache.get_redis", lambda: broken)

    response = client.get("/centres")

    assert response.status_code == 200
    assert response.json()["total"] == 2


def test_health_reports_dependencies(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


def test_request_id_is_generated_or_propagated(client):
    generated = client.get("/health")
    propagated = client.get("/health", headers={"X-Request-ID": "trace-abc"})

    assert len(generated.headers["X-Request-ID"]) == 32
    assert propagated.headers["X-Request-ID"] == "trace-abc"
