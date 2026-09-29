"""JWT auth: roles, failure modes, and tokens valid across replicas."""
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import token_headers


@pytest.fixture
def client(redis_server):
    with TestClient(create_app("replica-a")) as c:
        yield c


def test_login_issues_role_scoped_token(client):
    for user, pw, role in (("admin", "admin-pw", "admin"), ("viewer", "viewer-pw", "viewer")):
        body = client.post("/api/v1/auth/token", json={"username": user, "password": pw}).json()
        assert body["role"] == role and body["token_type"] == "bearer"
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}).json()
        assert me["username"] == user and me["role"] == role


@pytest.mark.parametrize("username,password", [("admin", "wrong"), ("nobody", "admin-pw"), ("admin", "")])
def test_bad_credentials_are_rejected(client, username, password):
    assert client.post("/api/v1/auth/token", json={"username": username, "password": password}).status_code == 401


def test_control_endpoints_need_admin(client):
    assert client.post("/api/v1/agents/a1/remediate").status_code == 401
    viewer = token_headers(client, "viewer", "viewer-pw")
    assert client.post("/api/v1/agents/a1/remediate", headers=viewer).status_code == 403
    assert client.post("/api/v1/alerts/1/resolve", headers=viewer).status_code == 403
    admin = token_headers(client)
    assert client.post("/api/v1/agents/a1/remediate", headers=admin).status_code == 404  # authorized; not connected


def test_forged_and_expired_tokens_are_rejected(client, monkeypatch):
    forged = jwt.encode({"sub": "admin", "role": "admin", "exp": int(time.time()) + 60}, "wrong-secret" * 4,
                        algorithm="HS256")
    resp = client.post("/api/v1/agents/a1/remediate", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401 and resp.json()["detail"] == "invalid token"

    import os
    expired = jwt.encode({"sub": "admin", "role": "admin", "exp": int(time.time()) - 5},
                         os.environ["JWT_SECRET"], algorithm="HS256")
    resp = client.post("/api/v1/agents/a1/remediate", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401 and resp.json()["detail"] == "token expired"


def test_token_from_one_replica_works_on_another(redis_server):
    with TestClient(create_app("replica-a")) as a, TestClient(create_app("replica-b")) as b:
        headers = token_headers(a)
        assert b.get("/api/v1/auth/me", headers=headers).json()["role"] == "admin"


def test_fails_closed_without_a_strong_secret(client, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short")
    assert client.post("/api/v1/auth/token", json={"username": "admin", "password": "admin-pw"}).status_code == 503
