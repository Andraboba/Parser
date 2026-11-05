"""Токены агентов и отсутствие секретов в репозитории и ответах API (ТЗ, п. 14 и 21)."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coordinator.api.app import create_app
from coordinator.config import Settings
from tests.conftest import ADMIN_LOGIN, PASSWORD
from tests.helpers import register, sql

ROOT = Path(__file__).resolve().parent.parent
REGISTER = {"name": "worker-01", "agent_version": "1", "protocol_version": 1, "threads": 1}

# Тексты, которых не должно быть в исходниках. Сама site-логика принадлежит агенту (раздел 18).
FORBIDDEN = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "закрытый ключ"),
    (re.compile(r"kinescope", re.I), "адрес внешнего источника"),
]
# Значения, похожие на настоящие секреты, а не на чтение настройки.
SECRET_ASSIGNMENT = re.compile(
    r"""(?:password|passwd|secret|token)["']?\s*[:=]\s*["'][0-9a-f]{24,}["']""", re.I,
)


def source_files() -> list[Path]:
    files = [
        *ROOT.glob("src/**/*.py"), *ROOT.glob("migrations/**/*.py"), *ROOT.glob("stub/*.py"),
        *ROOT.glob("web/src/**/*.ts*"), *ROOT.glob("deploy/**/*.y*ml"), *ROOT.glob("*.yaml"),
        *ROOT.glob("scripts/*"),
    ]
    assert len(files) > 30, "проверка должна видеть исходники"
    return [f for f in files if f.is_file()]


@pytest.mark.parametrize(("pattern", "what"), FORBIDDEN)
def test_sources_do_not_contain(pattern: re.Pattern[str], what: str) -> None:
    this_file = Path(__file__).resolve()
    hits = [
        str(f.relative_to(ROOT)) for f in source_files()
        if f.resolve() != this_file
        and pattern.search(f.read_text(encoding="utf-8", errors="ignore"))
    ]
    # Маскирование и проверка ключа обязаны упоминать признак закрытого ключа.
    hits = [h for h in hits if "security.py" not in h and "init-env" not in h]
    assert not hits, f"{what}: {hits}"


def test_sources_have_no_hardcoded_secrets() -> None:
    hits = [
        str(f.relative_to(ROOT)) for f in source_files()
        if SECRET_ASSIGNMENT.search(f.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not hits, hits


def test_cookie_is_secure_with_tls(clean_db: None) -> None:
    with TestClient(create_app(Settings(cookie_secure=True)), base_url="https://testserver") as c:
        resp = c.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": PASSWORD})
    assert "Secure" in resp.headers["set-cookie"]


@pytest.mark.integration
class TestTokenRotation:
    def test_rotation_without_downtime(self, client: TestClient) -> None:
        old = client.post("/api/agent-tokens", json={"worker_name": "worker-01"}).json()
        new = client.post("/api/agent-tokens", json={"worker_name": "worker-01"}).json()
        for token in (old["token"], new["token"]):
            assert client.post(
                "/api/workers/register", json=REGISTER, headers={"X-Agent-Token": token},
            ).status_code == 201, "пока агент не перезапущен, работают оба токена"

        assert client.post(f"/api/agent-tokens/{old['id']}/revoke").status_code == 200
        assert client.post(
            "/api/workers/register", json=REGISTER, headers={"X-Agent-Token": old["token"]},
        ).status_code == 401
        assert client.post(
            "/api/workers/register", json=REGISTER, headers={"X-Agent-Token": new["token"]},
        ).status_code == 201

    def test_token_value_is_shown_once_and_stored_as_hash(self, client: TestClient) -> None:
        issued = client.post("/api/agent-tokens", json={"worker_name": "worker-01"}).json()
        token = issued["token"]
        assert token not in client.get("/api/agent-tokens").text
        assert sql("SELECT count(*) FROM agent_tokens WHERE token_hash = :t", t=token) == [(0,)]

    def test_expired_token_is_rejected(self, client: TestClient) -> None:
        issued = client.post(
            "/api/agent-tokens", json={"worker_name": "worker-01", "expires_in_days": 1},
        ).json()
        assert issued["expires_at"] is not None
        sql("UPDATE agent_tokens SET expires_at = now() - interval '1 minute' WHERE id = :id",
            id=issued["id"])
        assert client.post(
            "/api/workers/register", json=REGISTER, headers={"X-Agent-Token": issued["token"]},
        ).status_code == 401

    def test_revoke_unknown_returns_404(self, client: TestClient) -> None:
        resp = client.post("/api/agent-tokens/999/revoke")
        assert resp.status_code == 404
        assert resp.json()["code"] == "token_not_found"

    def test_only_admin_manages_tokens(self, client: TestClient) -> None:
        client.post("/api/users", json={"login": "op", "password": PASSWORD, "role": "operator"})
        other = TestClient(client.app)
        other.portal = client.portal
        resp = other.post("/api/auth/login", json={"login": "op", "password": PASSWORD})
        other.headers["X-CSRF-Token"] = resp.json()["csrf_token"]
        assert other.get("/api/agent-tokens").status_code == 403
        assert other.post("/api/agent-tokens", json={}).status_code == 403

    def test_issued_token_is_absent_from_audit(self, client: TestClient) -> None:
        token = client.post("/api/agent-tokens", json={}).json()["token"]
        assert token not in client.get("/api/audit").text


@pytest.mark.integration
class TestNoLeaks:
    def test_api_answers_contain_no_secrets(self, client: TestClient) -> None:
        register(client)
        client.post("/api/users", json={"login": "u1", "password": PASSWORD, "role": "viewer"})
        for path in ("/api/workers", "/api/users", "/api/audit", "/api/events",
                     "/api/agent-tokens", "/api/auth/me"):
            body = client.get(path).text
            assert PASSWORD not in body, path
            assert "token_hash" not in body, path
            assert "password_hash" not in body, path

    def test_settings_repr_is_not_used_in_errors(self, client: TestClient) -> None:
        settings = Settings(admin_password="very-secret-value")
        with TestClient(create_app(settings)) as other:
            assert "very-secret-value" not in other.get("/health").text
