"""Контракт агентского API (раздел 5 плана): токены, версии, коды ошибок, OpenAPI."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from coordinator.api.app import create_app
from coordinator.api.deps import check_protocol
from coordinator.config import Settings
from coordinator.errors import ProtocolMismatch
from coordinator.storage.tokens import hash_token, parse_configured_tokens

OPENAPI_FILE = Path(__file__).parent.parent / "docs" / "openapi.json"
REGISTER = {"name": "worker-01", "agent_version": "1", "protocol_version": 1, "threads": 2}


def add_token(worker_name: str | None, token: str, *, revoked: bool = False) -> None:
    engine = create_engine(Settings().database_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO agent_tokens (worker_name, token_hash, revoked_at) "
            "VALUES (:name, :hash, CASE WHEN :revoked THEN now() END)"
        ), {"name": worker_name, "hash": hash_token(token), "revoked": revoked})
    engine.dispose()


def test_openapi_artifact_matches_application() -> None:
    assert json.loads(OPENAPI_FILE.read_text(encoding="utf-8")) == create_app().openapi(), (
        "docs/openapi.json устарел: запустите scripts/export-openapi.sh"
    )


def test_openapi_documents_error_codes_for_agent_methods() -> None:
    paths = create_app().openapi()["paths"]
    responses = paths["/api/workers/{worker_id}/lease"]["post"]["responses"]
    assert {"200", "204", "401", "403", "404", "409", "422"} <= set(responses)


@pytest.mark.parametrize(("raw", "expected"), [
    ("", []),
    ("worker-01=a,*=b", [("worker-01", "a"), (None, "b")]),
    (" worker-01=a=b ,", [("worker-01", "a=b")]),
])
def test_parse_configured_tokens(raw: str, expected: list[tuple[str | None, str]]) -> None:
    assert parse_configured_tokens(raw) == expected


@pytest.mark.parametrize("raw", ["worker-01", "=a", "worker-01="])
def test_parse_configured_tokens_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_configured_tokens(raw)


def test_protocol_range_is_inclusive() -> None:
    settings = Settings(protocol_version=2, min_protocol_version=1)
    check_protocol(1, settings)
    check_protocol(2, settings)
    for unsupported in (0, 3):
        with pytest.raises(ProtocolMismatch):
            check_protocol(unsupported, settings)


@pytest.mark.integration
class TestTokens:
    def test_missing_token_returns_401_with_code(self, client: TestClient) -> None:
        del client.headers["X-Agent-Token"]
        resp = client.post("/api/workers/register", json=REGISTER)
        assert resp.status_code == 401
        assert resp.json()["code"] == "token_required"

    def test_unknown_token_returns_401(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workers/register", json=REGISTER, headers={"X-Agent-Token": "nope"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "token_invalid"

    def test_revoked_token_returns_401(self, client: TestClient) -> None:
        add_token(None, "old", revoked=True)
        resp = client.post(
            "/api/workers/register", json=REGISTER, headers={"X-Agent-Token": "old"},
        )
        assert resp.status_code == 401

    def test_named_token_registers_only_its_worker(self, client: TestClient) -> None:
        add_token("worker-01", "own")
        headers = {"X-Agent-Token": "own"}
        assert client.post(
            "/api/workers/register", json=REGISTER, headers=headers,
        ).status_code == 201
        resp = client.post(
            "/api/workers/register", json={**REGISTER, "name": "worker-02"}, headers=headers,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "token_forbidden"

    def test_named_token_cannot_act_for_other_worker(self, client: TestClient) -> None:
        add_token("worker-01", "own")
        other = client.post(
            "/api/workers/register", json={**REGISTER, "name": "worker-02"},
        ).json()["worker_id"]
        resp = client.post(
            f"/api/workers/{other}/lease", json={}, headers={"X-Agent-Token": "own"},
        )
        assert resp.status_code == 403

    def test_token_is_checked_before_worker_lookup(self, client: TestClient) -> None:
        del client.headers["X-Agent-Token"]
        assert client.post("/api/workers/999/lease", json={}).status_code == 401


@pytest.mark.integration
class TestProtocolVersion:
    def test_register_rejects_unsupported_version(self, client: TestClient) -> None:
        resp = client.post("/api/workers/register", json={**REGISTER, "protocol_version": 99})
        assert resp.status_code == 409
        assert resp.json()["code"] == "protocol_version_mismatch"
        assert "99" in resp.json()["detail"]

    def test_header_with_unsupported_version_is_rejected(self, client: TestClient) -> None:
        worker_id = client.post("/api/workers/register", json=REGISTER).json()["worker_id"]
        resp = client.post(
            f"/api/workers/{worker_id}/lease", json={}, headers={"X-Protocol-Version": "99"},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "protocol_version_mismatch"


@pytest.mark.integration
def test_validation_error_has_code(client: TestClient) -> None:
    resp = client.post("/api/workers/register", json={"name": "x"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["detail"], list)


@pytest.mark.integration
def test_domain_errors_have_codes(client: TestClient) -> None:
    assert client.get("/api/jobs/999").json()["code"] == "job_not_found"
    assert client.post("/api/workers/999/lease", json={}).json()["code"] == "worker_not_found"
