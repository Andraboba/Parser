"""Серверы по SSH на стороне координатора: заведение, установка, сверка состояния."""
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coordinator.api.app import create_app
from coordinator.bootstrap import read_servers, seed_servers
from coordinator.config import Settings
from coordinator.domain.alerts import AlertLimits, WorkerSnapshot, worker_alerts
from coordinator.errors import CoordinatorError
from coordinator.provisioning import reconcile_servers
from coordinator.runner_client import RunnerClient, RunnerRejected
from coordinator.storage import db
from tests.conftest import FLEET_TOKEN, PASSWORD, login
from tests.helpers import must_lease, register, run_dispatcher_tick, sql, start_job

pytestmark = pytest.mark.integration

SERVER = {"name": "lab-srv-1", "host": "lab-srv-1", "threads": 8}
AGENT = {"name": "lab-srv-1", "agent_version": "1.0", "protocol_version": 1, "threads": 8}


class FakeRunner(RunnerClient):
    """Вместо runner-а: записывает вызовы и изображает сервер."""

    def __init__(self) -> None:
        super().__init__("http://runner", "t")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status = {"installed": False, "running": False, "pid": None, "release": None,
                       "python": "Python 3.13", "kernel": "6.1"}
        self.fail: CoordinatorError | None = None
        self.on_restart: Any = None

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        body = kwargs.get("json", {})
        self.calls.append((path, body))
        if self.fail is not None:
            raise self.fail
        if path == "/ssh/status":
            return self.status
        if path == "/ssh/deploy":
            self.status = {**self.status, "installed": True, "release": "real-abc"}
            return {"release": "real-abc"}
        if path == "/ssh/start":
            self.status = {**self.status, "running": True, "pid": 42}
        if path == "/ssh/stop":
            self.status = {**self.status, "running": False, "pid": None}
        if path == "/ssh/restart" and self.on_restart:
            self.on_restart()
        if path == "/ssh/logs":
            return {"logs": "строка"}
        if path == "/ssh/servers":
            return {"server": body["name"], "host": f"lab-{body['name']}", "id": "abc"}
        return None

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        threads_per_chunk=1, image_update_timeout=1, agent_site_template="http://site/{id}",
    )


@pytest.fixture
def api(clean_db: None, runner: FakeRunner, settings: Settings) -> Any:
    with TestClient(create_app(settings, runner=runner)) as client:
        client.headers["X-Agent-Token"] = FLEET_TOKEN
        login(client)
        yield client


def reconcile(runner: FakeRunner, settings: Settings) -> None:
    async def run() -> None:
        engine = db.make_engine(settings.database_url)
        try:
            async with db.make_sessionmaker(engine)() as session:
                await reconcile_servers(session, runner, settings)
        finally:
            await engine.dispose()

    asyncio.run(run())


def create(api: TestClient, **fields: Any) -> dict[str, Any]:
    resp = api.post("/api/servers", json={**SERVER, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    def test_parameters_from_requirements_are_stored(self, api: TestClient) -> None:
        card = create(
            api, ssh_port=2222, ssh_user="downloader", ssh_key="worker-key", threads=16,
            storage_path="/data/x", min_free_space_gb=20, agent_kind="stub",
        )
        assert card["provisioning_mode"] == "ssh"
        assert card["ssh_host"] == "lab-srv-1"
        assert (card["ssh_port"], card["ssh_user"]) == (2222, "downloader")
        assert card["ssh_key_ref"] == "worker-key"
        assert (card["threads"], card["storage_path"]) == (16, "/data/x")
        assert card["min_free_space_gb"] == 20
        assert card["agent_kind"] == "stub"
        assert card["status"] == "offline"
        assert card["desired_agent_state"] == "running"

    def test_duplicate_name_is_conflict(self, api: TestClient) -> None:
        create(api)
        assert api.post("/api/servers", json=SERVER).status_code == 409

    @pytest.mark.parametrize("fields", [
        {"name": "Bad Name"}, {"ssh_port": 70000}, {"ssh_user": "root; rm"},
        {"ssh_key": "../key"}, {"ssh_key": "/run/secrets/x"}, {"agent_kind": "evil"},
        {"storage_path": "relative"}, {"host": None},
    ])
    def test_invalid_parameters_return_422(
        self, api: TestClient, fields: dict[str, Any],
    ) -> None:
        assert api.post("/api/servers", json={**SERVER, **fields}).status_code == 422

    def test_container_can_be_created_for_stand(
        self, api: TestClient, runner: FakeRunner,
    ) -> None:
        card = create(api, host=None, create_container=True, cpus=1.5, memory_mb=512)
        assert card["ssh_host"] == "lab-lab-srv-1"
        path, body = runner.calls[0]
        assert path == "/ssh/servers"
        assert (body["cpus"], body["memory_mb"]) == (1.5, 512)

    def test_host_outside_allowlist_leaves_no_row(self, api: TestClient) -> None:
        resp = api.post("/api/servers", json={**SERVER, "host": "evil.example"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "host_not_allowed"
        assert not [w for w in api.get("/api/workers").json() if w["name"] == SERVER["name"]]

    def test_only_administrator_may_create(self, api: TestClient) -> None:
        api.post("/api/users", json={"login": "op", "password": PASSWORD, "role": "operator"})
        other = TestClient(api.app)
        other.portal = api.portal
        login(other, "op")
        assert other.post("/api/servers", json=SERVER).status_code == 403


class TestRegistration:
    def test_agent_registration_keeps_server_mode_and_address(self, api: TestClient) -> None:
        card = create(api)
        resp = api.post("/api/workers/register", json=AGENT)
        assert resp.json()["worker_id"] == card["id"]
        after = api.get(f"/api/workers/{card['id']}").json()
        assert after["provisioning_mode"] == "ssh", "агент о своём режиме не знает"
        assert after["ssh_host"] == "lab-srv-1"
        assert after["status"] == "online"
        assert after["agent_version"] == "1.0"

    def test_plain_worker_stays_docker(self, api: TestClient) -> None:
        worker = register(api, "plain")
        assert api.get(f"/api/workers/{worker}").json()["provisioning_mode"] == "docker"


class TestReconcile:
    def test_new_server_gets_agent_installed_and_started(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        card = create(api)
        reconcile(runner, settings)
        assert runner.paths() == ["/ssh/status", "/ssh/deploy", "/ssh/start"]
        _, deploy = runner.calls[1]
        assert deploy["target"]["host"] == "lab-srv-1"
        assert deploy["kind"] == "real"
        state = api.get(f"/api/workers/{card['id']}").json()["container_state"]
        assert state["image"] == "real-abc"
        env = deploy["env"]
        assert env["AGENT_NAME"] == "lab-srv-1"
        assert env["AGENT_SITE_TEMPLATE"] == "http://site/{id}"
        assert env["SSL_CERT_FILE"] == settings.agent_ca_file
        # Выданный токен рабочий и привязан к этому имени.
        token = env["AGENT_TOKEN"]
        assert api.post(
            "/api/workers/register", json=AGENT, headers={"X-Agent-Token": token},
        ).status_code == 201
        assert api.post(
            "/api/workers/register", json={**AGENT, "name": "other"},
            headers={"X-Agent-Token": token},
        ).status_code == 403
        assert card["id"]

    def test_token_never_appears_in_answers_or_journal(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        card = create(api)
        reconcile(runner, settings)
        token = runner.calls[1][1]["env"]["AGENT_TOKEN"]
        for path in (f"/api/workers/{card['id']}", "/api/events", "/api/audit", "/api/workers"):
            assert token not in api.get(path).text

    def test_running_agent_is_left_alone(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        create(api)
        runner.status = {**runner.status, "installed": True, "running": True, "pid": 7}
        reconcile(runner, settings)
        assert runner.paths() == ["/ssh/status"]

    def test_crashed_agent_is_restarted_and_counted(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        card = create(api)
        runner.status = {**runner.status, "installed": True, "running": True, "pid": 7}
        reconcile(runner, settings)
        runner.status = {**runner.status, "running": False, "pid": None}
        reconcile(runner, settings)
        assert runner.paths()[-1] == "/ssh/start"
        state = api.get(f"/api/workers/{card['id']}").json()["container_state"]
        assert state["restart_count"] == 1
        events = api.get("/api/events", params={"operation": "agent.start"}).json()
        assert "после сбоя" in events[0]["message"]

    def test_stopped_by_operator_is_not_restarted(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        card = create(api)
        runner.status = {**runner.status, "installed": True, "running": True, "pid": 7}
        api.post(f"/api/workers/{card['id']}/agent/stop")
        runner.calls.clear()
        reconcile(runner, settings)
        assert "/ssh/start" not in runner.paths()
        after = api.get(f"/api/workers/{card['id']}").json()
        assert after["desired_agent_state"] == "stopped"
        assert after["container_state"]["state"] == "stopped"

    def test_server_created_without_start_stays_down(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        create(api, start=False)
        reconcile(runner, settings)
        assert runner.paths() == ["/ssh/status"]

    def test_unreachable_server_frees_chunk_at_once(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        card = create(api)
        start_job(api)
        api.post("/api/workers/register", json=AGENT)
        chunk = must_lease(api, card["id"])
        runner.fail = RunnerRejected(502, "ssh_unreachable", "сервер недоступен")
        reconcile(runner, settings)

        after = api.get(f"/api/workers/{card['id']}").json()
        assert after["status"] == "offline"
        assert after["container_state"]["state"] == "unreachable"
        run_dispatcher_tick()
        assert must_lease(api, register(api, "other"))["chunk_id"] == chunk["chunk_id"]

    def test_failed_start_is_reported_and_retried_next_time(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        card = create(api)
        runner.status = {**runner.status, "installed": True}
        original = runner.call

        async def failing(method: str, path: str, **kwargs: Any) -> Any:
            if path == "/ssh/start":
                raise RunnerRejected(502, "ssh_command_failed", "нет python3")
            return await original(method, path, **kwargs)

        runner.call = failing  # type: ignore[method-assign]
        reconcile(runner, settings)
        state = api.get(f"/api/workers/{card['id']}").json()["container_state"]
        assert "нет python3" in state["error"]
        events = api.get("/api/events", params={"operation": "agent.start"}).json()
        assert events[0]["result"] == "failed"
        runner.call = original  # type: ignore[method-assign]
        reconcile(runner, settings)
        state = api.get(f"/api/workers/{card['id']}").json()["container_state"]
        assert state["state"] == "starting"

    def test_docker_workers_are_not_touched(
        self, api: TestClient, runner: FakeRunner, settings: Settings,
    ) -> None:
        register(api, "plain")
        reconcile(runner, settings)
        assert runner.calls == []


class TestAgentControl:
    def test_operator_controls_agent(self, api: TestClient, runner: FakeRunner) -> None:
        card = create(api)
        base = f"/api/workers/{card['id']}/agent"
        assert api.post(f"{base}/stop").json()["desired_agent_state"] == "stopped"
        assert api.post(f"{base}/start").json()["desired_agent_state"] == "running"
        assert api.post(f"{base}/restart").status_code == 200
        assert api.get(f"{base}/logs").json() == {"logs": "строка"}
        assert ["/ssh/stop", "/ssh/start", "/ssh/restart", "/ssh/logs"] == runner.paths()

    def test_check_reports_unreachable_as_answer(
        self, api: TestClient, runner: FakeRunner,
    ) -> None:
        card = create(api)
        base = f"/api/workers/{card['id']}/agent"
        assert api.post(f"{base}/check").json()["reachable"] is True
        runner.fail = RunnerRejected(409, "host_key_changed", "ключ изменился")
        body = api.post(f"{base}/check").json()
        assert body["reachable"] is False
        assert body["error_code"] == "host_key_changed"

    def test_deploy_succeeds_when_agent_registers(
        self, api: TestClient, runner: FakeRunner,
    ) -> None:
        card = create(api)
        runner.on_restart = lambda: sql(
            "UPDATE workers SET status = 'online', last_heartbeat_at = now() + interval '1 second'"
        )
        resp = api.post(f"/api/workers/{card['id']}/agent/deploy", json={"agent_kind": "stub"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["agent_kind"] == "stub"
        assert "/ssh/rollback" not in runner.paths()

    def test_deploy_rolls_back_when_agent_stays_silent(
        self, api: TestClient, runner: FakeRunner,
    ) -> None:
        card = create(api)
        resp = api.post(f"/api/workers/{card['id']}/agent/deploy", json={})
        assert resp.status_code == 502
        assert resp.json()["code"] == "update_rolled_back"
        assert runner.paths()[-1] == "/ssh/rollback"

    def test_each_deploy_replaces_token(self, api: TestClient, runner: FakeRunner) -> None:
        card = create(api)
        runner.on_restart = lambda: sql(
            "UPDATE workers SET status = 'online', last_heartbeat_at = now() + interval '1 second'"
        )
        base = f"/api/workers/{card['id']}/agent/deploy"
        api.post(base, json={})
        first = next(b for p, b in runner.calls if p == "/ssh/deploy")["env"]["AGENT_TOKEN"]
        api.post(base, json={})
        assert api.post(
            "/api/workers/register", json=AGENT, headers={"X-Agent-Token": first},
        ).status_code == 401

    def test_plain_worker_has_no_ssh_control(self, api: TestClient) -> None:
        worker = register(api, "plain")
        resp = api.post(f"/api/workers/{worker}/agent/start")
        assert resp.status_code == 409
        assert resp.json()["code"] == "not_ssh_worker"

    def test_unknown_worker_returns_404(self, api: TestClient) -> None:
        assert api.post("/api/workers/999/agent/start").status_code == 404

    def test_viewer_has_no_control(self, api: TestClient) -> None:
        card = create(api)
        api.post("/api/users", json={"login": "v", "password": PASSWORD, "role": "viewer"})
        other = TestClient(api.app)
        other.portal = api.portal
        login(other, "v")
        base = f"/api/workers/{card['id']}/agent"
        assert other.post(f"{base}/stop").status_code == 403
        assert other.get(f"{base}/logs").status_code == 403
        assert other.post(f"{base}/deploy", json={}).status_code == 403

    def test_operator_cannot_deploy(self, api: TestClient) -> None:
        card = create(api)
        api.post("/api/users", json={"login": "op", "password": PASSWORD, "role": "operator"})
        other = TestClient(api.app)
        other.portal = api.portal
        login(other, "op")
        assert other.post(f"/api/workers/{card['id']}/agent/deploy", json={}).status_code == 403


class TestSeed:
    def test_servers_from_config_are_created_once(self, clean_db: None, tmp_path: Path) -> None:
        path = tmp_path / "servers.json"
        path.write_text(json.dumps([
            {"name": "a", "host": "a", "ssh_port": 2222, "ssh_user": "u", "ssh_key": "k",
             "threads": 8, "storage_path": "/data", "min_free_space_gb": 20},
        ]), encoding="utf-8")

        async def seed() -> None:
            settings = Settings(agent_kind="stub")
            engine = db.make_engine(settings.database_url)
            try:
                async with db.make_sessionmaker(engine)() as session:
                    await seed_servers(session, read_servers(path), settings)
            finally:
                await engine.dispose()

        asyncio.run(seed())
        sql("UPDATE workers SET threads = 99")
        asyncio.run(seed())
        assert sql(
            "SELECT provisioning_mode, ssh_port, ssh_key_ref, threads, agent_kind, "
            "desired_agent_state FROM workers"
        ) == [("ssh", 2222, "k", 99, "stub", "running")]


@pytest.mark.parametrize(("state", "alert"), [
    ("unreachable", True), ("stopped", True), ("running", False),
])
def test_server_state_raises_alert(state: str, alert: bool) -> None:
    node = WorkerSnapshot(
        id=1, name="srv", enabled=True, online=state == "running", heartbeat_age_seconds=1.0,
        agent_version="1", protocol_version=1, container_state=state,
    )
    codes = {a.code for a in worker_alerts(node, AlertLimits(), 11.0)}
    assert ("worker_unavailable" in codes) is alert


def test_stopped_by_operator_raises_no_alert(api: TestClient, runner: FakeRunner) -> None:
    card = create(api)
    api.post(f"/api/workers/{card['id']}/agent/stop")
    assert api.get("/api/alerts").json() == []
