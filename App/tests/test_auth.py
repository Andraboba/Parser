"""Аутентификация, роли, CSRF, аудит, журнал событий, маскирование (ТЗ, п. 14 и 15)."""
import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from coordinator.api.app import create_app
from coordinator.config import Settings
from coordinator.domain.security import (
    Role,
    hash_password,
    mask_secrets,
    role_allows,
    verify_password,
)
from coordinator.logging_setup import JsonFormatter
from tests.conftest import ADMIN_LOGIN, FLEET_TOKEN, PASSWORD, login
from tests.helpers import (
    expire_leases,
    must_lease,
    register,
    run_dispatcher_tick,
    silence_worker,
    sql,
    start_job,
)


def test_password_hash_roundtrip() -> None:
    stored = hash_password("секретный пароль")
    assert verify_password("секретный пароль", stored)
    assert not verify_password("другой", stored)
    assert "секретный" not in stored
    assert stored != hash_password("секретный пароль"), "соль у каждого хэша своя"


@pytest.mark.parametrize("stored", ["", "bad", "md5$1$2$3$4$5", "scrypt$x$y$z$a$b"])
def test_verify_rejects_malformed_hash(stored: str) -> None:
    assert not verify_password("x", stored)


@pytest.mark.parametrize(("role", "required", "allowed"), [
    (Role.VIEWER, Role.VIEWER, True), (Role.VIEWER, Role.OPERATOR, False),
    (Role.OPERATOR, Role.VIEWER, True), (Role.OPERATOR, Role.ADMINISTRATOR, False),
    (Role.ADMINISTRATOR, Role.OPERATOR, True),
])
def test_role_ranks(role: Role, required: Role, allowed: bool) -> None:
    assert role_allows(role, required) is allowed


@pytest.mark.parametrize(("raw", "secret"), [
    ("postgresql+psycopg://coord:s3cret@db:5432/coord", "s3cret"),
    ("Authorization: Bearer abc.def-123", "abc.def-123"),
    ("token=abc123", "abc123"),
    ('{"password": "qwerty"}', "qwerty"),
    ("api_key: k-1", "k-1"),
    ("X-Agent-Token: tok-1", "tok-1"),
    ("cookie=coord_session_value", "coord_session_value"),
    ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "abc"),
])
def test_secrets_are_masked(raw: str, secret: str) -> None:
    assert secret not in mask_secrets(raw)


def test_ordinary_text_is_not_changed() -> None:
    text_ = "блок 1..100 закрыт, контрольная точка 101, http://site/page/5"
    assert mask_secrets(text_) == text_


def test_json_log_has_required_fields_and_masks_secrets() -> None:
    record = logging.LogRecord(
        "x", logging.WARNING, __file__, 1, "ошибка password=hunter2", None, None,
    )
    record.job_id = 7
    record.operation = "chunk.fail"
    entry = json.loads(JsonFormatter("api").format(record))
    assert entry["level"] == "WARNING"
    assert entry["component"] == "api"
    assert entry["job_id"] == 7
    assert entry["operation"] == "chunk.fail"
    assert entry["time"].endswith("+00:00"), "время в UTC"
    assert "hunter2" not in entry["message"]


def add_user(client: TestClient, name: str, role: str) -> int:
    resp = client.post("/api/users", json={"login": name, "password": PASSWORD, "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
def anonymous(clean_db: None) -> Any:
    with TestClient(create_app(Settings())) as client:
        yield client


def second_client(client: TestClient) -> TestClient:
    """Другой браузер того же приложения: те же подключения и тот же цикл событий."""
    other = TestClient(client.app)
    other.portal = client.portal
    return other


def as_role(client: TestClient, role: str) -> TestClient:
    add_user(client, role, role)
    other = second_client(client)
    login(other, role)
    return other


@pytest.mark.integration
class TestLogin:
    def test_login_sets_protected_cookie_and_returns_role(self, anonymous: TestClient) -> None:
        resp = anonymous.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": PASSWORD})
        assert resp.status_code == 200
        assert resp.json()["role"] == "administrator"
        cookie = resp.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "coord_session=" in cookie
        assert PASSWORD not in resp.text

    def test_session_token_is_stored_only_as_hash(self, anonymous: TestClient) -> None:
        anonymous.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": PASSWORD})
        cookie = anonymous.cookies["coord_session"]
        assert sql("SELECT count(*) FROM sessions WHERE token_hash = :c", c=cookie) == [(0,)]
        assert sql("SELECT count(*) FROM sessions") == [(1,)]

    @pytest.mark.parametrize(("name", "password"), [
        (ADMIN_LOGIN, "wrong-password"), ("ghost", PASSWORD),
    ])
    def test_wrong_credentials_get_same_answer(
        self, anonymous: TestClient, name: str, password: str,
    ) -> None:
        resp = anonymous.post("/api/auth/login", json={"login": name, "password": password})
        assert resp.status_code == 401
        assert resp.json() == {
            "detail": "неверный логин или пароль", "code": "invalid_credentials",
        }

    def test_too_many_failures_lock_login(self, anonymous: TestClient) -> None:
        for _ in range(5):
            anonymous.post("/api/auth/login", json={"login": "locked", "password": "x"})
        resp = anonymous.post("/api/auth/login", json={"login": "locked", "password": "x"})
        assert resp.status_code == 429
        assert resp.json()["code"] == "too_many_attempts"

    def test_success_resets_failures(self, anonymous: TestClient) -> None:
        for _ in range(4):
            anonymous.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": "x"})
        login(anonymous)
        anonymous.post("/api/auth/logout")
        for _ in range(4):
            anonymous.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": "x"})
        login(anonymous)

    def test_logout_ends_session(self, client: TestClient) -> None:
        cookie = client.cookies["coord_session"]
        assert client.post("/api/auth/logout").status_code == 204
        client.cookies.set("coord_session", cookie)
        assert client.get("/api/auth/me").status_code == 401

    def test_expired_session_is_rejected(self, client: TestClient) -> None:
        sql("UPDATE sessions SET expires_at = now() - interval '1 minute'")
        assert client.get("/api/auth/me").status_code == 401


@pytest.mark.integration
class TestProtection:
    @pytest.mark.parametrize(("method", "path"), [
        ("get", "/api/jobs"), ("get", "/api/jobs/1"), ("post", "/api/jobs"),
        ("post", "/api/jobs/1/start"), ("post", "/api/jobs/1/stop"),
        ("get", "/api/jobs/1/items"), ("get", "/api/jobs/1/items.csv"),
        ("get", "/api/workers"), ("get", "/api/workers/1"), ("patch", "/api/workers/1"),
        ("get", "/api/workers/1/metrics"), ("get", "/api/rate-limit"), ("get", "/api/events"),
        ("get", "/api/containers"), ("post", "/api/containers"), ("get", "/api/users"),
        ("get", "/api/audit"), ("post", "/api/workers/1/container/stop"),
    ])
    def test_interface_requires_login(
        self, anonymous: TestClient, method: str, path: str,
    ) -> None:
        resp = getattr(anonymous, method)(path, **({} if method == "get" else {"json": {}}))
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code}"
        assert resp.json()["code"] == "not_authenticated"

    def test_every_api_route_is_protected(self, anonymous: TestClient) -> None:
        """Новый маршрут без проверки прав не должен пройти незамеченным."""
        public = {"/api/auth/login", "/api/auth/logout"}
        schema = anonymous.get("/openapi.json").json()
        for path, methods in schema["paths"].items():
            if not path.startswith("/api/") or path in public:
                continue
            url = path
            for parameter in ("{job_id}", "{worker_id}", "{user_id}"):
                url = url.replace(parameter, "1")
            for method in methods:
                resp = anonymous.request(method.upper(), url, json={})
                assert resp.status_code in (401, 422), f"{method} {path}: {resp.status_code}"

    def test_health_stays_public(self, anonymous: TestClient) -> None:
        assert anonymous.get("/health").status_code == 200

    def test_agent_routes_do_not_accept_user_session(self, client: TestClient) -> None:
        del client.headers["X-Agent-Token"]
        resp = client.post("/api/workers/register", json={
            "name": "x", "agent_version": "1", "protocol_version": 1, "threads": 1,
        })
        assert resp.status_code == 401
        assert resp.json()["code"] == "token_required"

    def test_agent_token_does_not_open_interface(self, anonymous: TestClient) -> None:
        anonymous.headers["X-Agent-Token"] = FLEET_TOKEN
        assert anonymous.get("/api/jobs").status_code == 401

    def test_write_without_csrf_token_is_rejected(self, client: TestClient) -> None:
        del client.headers["X-CSRF-Token"]
        resp = client.post("/api/jobs", json={"range": "1"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "csrf_failed"

    def test_write_with_wrong_csrf_token_is_rejected(self, client: TestClient) -> None:
        client.headers["X-CSRF-Token"] = "wrong"
        assert client.post("/api/jobs", json={"range": "1"}).status_code == 403

    def test_read_does_not_need_csrf_token(self, client: TestClient) -> None:
        del client.headers["X-CSRF-Token"]
        assert client.get("/api/jobs").status_code == 200


@pytest.mark.integration
class TestRoles:
    READ = [("get", "/api/jobs"), ("get", "/api/workers"), ("get", "/api/events")]

    @pytest.mark.parametrize(("method", "path"), READ)
    def test_viewer_can_read(self, client: TestClient, method: str, path: str) -> None:
        viewer = as_role(client, "viewer")
        assert getattr(viewer, method)(path).status_code == 200

    def test_viewer_cannot_change_anything(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        viewer = as_role(client, "viewer")
        for method, path, payload in [
            ("post", "/api/jobs", {"range": "1"}),
            ("post", f"/api/jobs/{job_id}/stop", None),
            ("patch", f"/api/jobs/{job_id}", {"threads": 1}),
            ("post", f"/api/jobs/{job_id}/retry-errors", None),
            ("patch", f"/api/workers/{worker}", {"enabled": False}),
            ("post", f"/api/workers/{worker}/container/stop", None),
            ("get", f"/api/workers/{worker}/container/logs", None),
            ("post", "/api/containers", {}),
            ("post", "/api/users", {}),
        ]:
            resp = getattr(viewer, method)(path, **({"json": payload} if payload else {}))
            assert resp.status_code == 403, f"{method} {path}: {resp.status_code}"
            assert resp.json()["code"] == "permission_denied"

    def test_operator_manages_jobs_and_workers(self, client: TestClient) -> None:
        worker = register(client)
        operator = as_role(client, "operator")
        job = operator.post("/api/jobs", json={"range": "1"})
        assert job.status_code == 201
        assert operator.post(f"/api/jobs/{job.json()['id']}/start").status_code == 200
        assert operator.patch(f"/api/workers/{worker}", json={"enabled": False}).status_code == 200

    def test_operator_cannot_administer(self, client: TestClient) -> None:
        worker = register(client)
        operator = as_role(client, "operator")
        assert operator.get("/api/users").status_code == 403
        assert operator.get("/api/audit").status_code == 403
        assert operator.post("/api/containers", json={}).status_code == 403
        resp = operator.post(
            f"/api/workers/{worker}/container/update-image", json={"image": "x"},
        )
        assert resp.status_code == 403


@pytest.mark.integration
class TestUsers:
    def test_password_is_never_returned(self, client: TestClient) -> None:
        add_user(client, "user1", "viewer")
        assert "password" not in json.dumps(client.get("/api/users").json())

    def test_short_password_is_rejected(self, client: TestClient) -> None:
        resp = client.post("/api/users", json={"login": "u", "password": "short", "role": "viewer"})
        assert resp.status_code == 422

    @pytest.mark.parametrize("name", ["a b", "<script>", "", "x" * 65])
    def test_bad_login_is_rejected(self, client: TestClient, name: str) -> None:
        resp = client.post(
            "/api/users", json={"login": name, "password": PASSWORD, "role": "viewer"},
        )
        assert resp.status_code == 422

    def test_duplicate_login_is_conflict(self, client: TestClient) -> None:
        add_user(client, "user1", "viewer")
        resp = client.post(
            "/api/users", json={"login": "user1", "password": PASSWORD, "role": "viewer"},
        )
        assert resp.status_code == 409

    def test_changing_role_ends_user_sessions(self, client: TestClient) -> None:
        user_id = add_user(client, "user1", "operator")
        other = second_client(client)
        login(other, "user1")
        client.patch(f"/api/users/{user_id}", json={"role": "viewer"})
        assert other.get("/api/auth/me").status_code == 401
        login(other, "user1")
        assert other.get("/api/auth/me").json()["role"] == "viewer"

    def test_disabled_user_cannot_login(self, client: TestClient) -> None:
        user_id = add_user(client, "user1", "viewer")
        client.patch(f"/api/users/{user_id}", json={"disabled": True})
        other = second_client(client)
        resp = other.post("/api/auth/login", json={"login": "user1", "password": PASSWORD})
        assert resp.status_code == 401

    def test_password_change_works(self, client: TestClient) -> None:
        user_id = add_user(client, "user1", "viewer")
        client.patch(f"/api/users/{user_id}", json={"password": "new-password-1"})
        other = second_client(client)
        assert other.post(
            "/api/auth/login", json={"login": "user1", "password": PASSWORD},
        ).status_code == 401
        login(other, "user1", "new-password-1")

    @pytest.mark.parametrize("change", [{"role": "viewer"}, {"disabled": True}])
    def test_last_admin_is_protected(self, client: TestClient, change: dict[str, Any]) -> None:
        admin_id = client.get("/api/users").json()[0]["id"]
        resp = client.patch(f"/api/users/{admin_id}", json=change)
        assert resp.status_code == 409
        assert resp.json()["code"] == "last_admin"

    def test_second_admin_unlocks_demotion(self, client: TestClient) -> None:
        add_user(client, "admin2", "administrator")
        admin_id = next(u["id"] for u in client.get("/api/users").json() if u["login"] == "admin")
        assert client.patch(f"/api/users/{admin_id}", json={"role": "viewer"}).status_code == 200


@pytest.mark.integration
class TestAudit:
    def entries(self, client: TestClient, **params: Any) -> list[dict[str, Any]]:
        return client.get("/api/audit", params=params).json()

    def test_user_actions_are_recorded_without_secrets(self, client: TestClient) -> None:
        job_id = start_job(client)
        client.post(f"/api/jobs/{job_id}/stop")
        add_user(client, "user1", "viewer")
        rows = self.entries(client)
        actions = {r["action"] for r in rows}
        assert "POST /api/jobs/{job_id}/stop" in actions
        assert "POST /api/users" in actions
        stop = next(r for r in rows if r["action"].endswith("/stop"))
        assert (stop["actor"], stop["object"], stop["result"]) == (
            "admin", f"job_id={job_id}", "200",
        )
        assert PASSWORD not in json.dumps(rows)

    def test_failed_login_is_recorded(self, anonymous: TestClient, client: TestClient) -> None:
        anonymous.post("/api/auth/login", json={"login": "someone", "password": "secret-value"})
        row = next(r for r in self.entries(client) if r["actor"] == "someone")
        assert row["action"] == "POST /api/auth/login"
        assert row["result"] == "401"
        assert "secret-value" not in json.dumps(row)

    def test_denied_action_is_recorded(self, client: TestClient) -> None:
        viewer = as_role(client, "viewer")
        viewer.post("/api/jobs", json={"range": "1"})
        row = next(r for r in self.entries(client) if r["actor"] == "viewer")
        assert row["result"] == "403"

    def test_agent_registration_is_recorded_but_heartbeat_is_not(
        self, client: TestClient,
    ) -> None:
        worker = register(client)
        client.post(f"/api/workers/{worker}/heartbeat", json={})
        actions = {r["action"] for r in self.entries(client)}
        assert "POST /api/workers/register" in actions
        assert "POST /api/workers/{worker_id}/heartbeat" not in actions

    def test_reading_is_not_recorded(self, client: TestClient) -> None:
        client.get("/api/jobs")
        assert not [r for r in self.entries(client) if r["action"].startswith("GET")]

    def test_filter_by_actor(self, client: TestClient) -> None:
        client.post("/api/jobs", json={"range": "1"})
        assert self.entries(client, actor="ghost") == []
        assert self.entries(client, actor="admin")

    @pytest.mark.parametrize("statement", [
        "UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log",
    ])
    def test_records_cannot_be_changed_even_in_database(
        self, client: TestClient, statement: str,
    ) -> None:
        client.post("/api/jobs", json={"range": "1"})
        engine = create_engine(Settings().database_url)
        with pytest.raises(DBAPIError, match="только добавляются"), engine.begin() as conn:
            conn.execute(text(statement))
        engine.dispose()


@pytest.mark.integration
class TestEvents:
    def search(self, client: TestClient, **params: Any) -> list[dict[str, Any]]:
        resp = client.get("/api/events", params=params)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_job_and_chunk_events_have_required_fields(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        client.post(f"/api/workers/{worker}/complete", json={
            "chunk_id": chunk["chunk_id"], "next_id": 11, "results": [],
        })
        rows = self.search(client, job_id=job_id)
        operations = [r["operation"] for r in rows]
        assert {"job.start", "chunk.lease", "chunk.complete", "job.status"} <= set(operations)
        lease = next(r for r in rows if r["operation"] == "chunk.lease")
        assert lease["worker_id"] == worker
        assert lease["chunk_id"] == chunk["chunk_id"]
        assert lease["result"] == "leased"
        assert lease["level"] == "info"
        assert lease["component"] == "api"

    def test_search_by_worker_operation_result_and_text(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        must_lease(client, worker)
        assert self.search(client, worker_id=worker)
        assert self.search(client, worker_id=999) == []
        assert self.search(client, operation="chunk.lease")
        assert self.search(client, result="leased")
        assert self.search(client, q="блок 1..10")
        assert self.search(client, q="нет такого") == []

    def test_search_by_time_interval(self, client: TestClient) -> None:
        start_job(client)
        assert self.search(client, since="2000-01-01T00:00:00Z", until="2100-01-01T00:00:00Z")
        assert self.search(client, since="2100-01-01T00:00:00Z") == []
        assert self.search(client, until="2000-01-01T00:00:00Z") == []

    def test_pagination_newest_first(self, client: TestClient) -> None:
        for _ in range(3):
            client.post("/api/jobs/999/start")
            start_job(client)
        rows = self.search(client, limit=2)
        assert len(rows) == 2
        assert rows[0]["id"] > rows[1]["id"]
        older = self.search(client, before_id=rows[1]["id"], limit=1)
        assert older[0]["id"] < rows[1]["id"]

    def test_job_events_endpoint_is_scoped(self, client: TestClient) -> None:
        first = start_job(client)
        second = start_job(client)
        rows = client.get(f"/api/jobs/{first}/events").json()
        assert rows
        assert {r["job_id"] for r in rows} == {first}
        assert second != first

    def test_rejected_file_is_logged_with_item(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        client.post(f"/api/workers/{worker}/complete", json={
            "chunk_id": chunk["chunk_id"], "next_id": 11, "results": [
                {"external_id": 3, "status": "completed",
                 "file": {"path": "/etc/passwd", "size": 1}},
            ],
        })
        row = self.search(client, job_id=job_id, operation="file.rejected")[0]
        assert row["external_id"] == 3
        assert row["error_code"] == "invalid_path"
        assert row["level"] == "error"

    def test_secret_in_agent_message_is_masked_in_journal(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        client.post(f"/api/workers/{worker}/fail", json={
            "chunk_id": chunk["chunk_id"], "error_class": "network",
            "message": "connect postgresql://u:hunter2@db failed, token=abc123",
        })
        row = self.search(client, operation="chunk.fail")[0]
        assert "hunter2" not in row["message"]
        assert "abc123" not in row["message"]
        assert row["error_code"] == "network"

    def test_dispatcher_records_reclaim_and_offline(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        must_lease(client, worker)
        expire_leases()
        run_dispatcher_tick()
        row = self.search(client, operation="chunk.reclaimed")[0]
        assert row["component"] == "dispatcher"
        assert row["error_code"] == "lease_expired"

        silence_worker(worker)
        run_dispatcher_tick()
        assert self.search(client, operation="worker.offline")[0]["worker_id"] == worker

    def test_job_status_change_by_condition_is_recorded(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,]", max_duration_seconds=1)
        sql("UPDATE jobs SET started_at = now() - interval '1 hour'")
        run_dispatcher_tick()
        rows = self.search(client, job_id=job_id, operation="job.stop_condition")
        assert rows[0]["message"] == "max_duration"
