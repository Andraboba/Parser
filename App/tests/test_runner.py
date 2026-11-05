"""Runner: ограничения и обновление образа на поддельном Docker Engine API."""
import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncssh
import httpx
import pytest
from fastapi.testclient import TestClient

from coordinator.api.app import create_app
from coordinator.config import Settings
from coordinator.dispatcher.ticks import sync_containers
from coordinator.runner.app import create_runner_app
from coordinator.runner.docker import DockerClient, demux_logs
from coordinator.runner.service import filter_env, image_allowed
from coordinator.runner.ssh import SshManager
from coordinator.runner_client import RunnerClient
from coordinator.storage import db
from tests.conftest import FLEET_TOKEN, login
from tests.helpers import must_lease, register, run_dispatcher_tick, sql, start_job

TOKEN = "runner-secret"


class FakeDocker:
    """Минимальная модель Docker Engine: контейнеры в памяти и журнал вызовов."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.images = {"coord/agent-stub:dev", "coord/agent-stub:new"}
        self.fail_create = False
        self.next_id = 0
        self.on_start: Any = None

    def add(self, worker: str, image: str = "coord/agent-stub:dev") -> str:
        self.next_id += 1
        cid = f"{self.next_id:064x}"
        self.containers[cid] = {
            "Id": cid, "Name": f"/coord-{worker}-1", "Image": image, "State": "running",
            "Status": "Up 1 minute", "Labels": {"lab.worker": worker},
            "Config": {"Hostname": cid[:12], "Image": image, "Cmd": ["python", "old.py"],
                       "Env": [f"AGENT_IMAGE={image}", "AGENT_TOKEN=secret", "PATH=/old"],
                       "Labels": {"lab.worker": worker}},
            "HostConfig": {"Memory": 1},
            "NetworkSettings": {"Networks": {"coord_default": {"Aliases": [worker]}}},
            "RestartCount": 2, "OOMKilled": False,
        }
        return cid

    def _inspect(self, cid: str) -> dict[str, Any]:
        c = self.containers[cid]
        return {**c, "State": {"Status": c["State"], "OOMKilled": c["OOMKilled"],
                               "StartedAt": "2026-01-01T00:00:00Z"}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        path, query = parts.path, parse_qs(parts.query)
        self.calls.append((request.method, path))
        if path == "/containers/json":
            label = json.loads(query["filters"][0])["label"][0]
            key, _, value = label.partition("=")
            found = [
                c for c in self.containers.values()
                if key in c["Labels"] and (not value or c["Labels"][key] == value)
                and not c["Name"].endswith("-previous")
            ]
            return httpx.Response(200, json=found)
        if path == "/containers/create":
            if self.fail_create:
                return httpx.Response(500, json={"message": "нет места"})
            body = json.loads(request.content)
            self.next_id += 1
            cid = f"{self.next_id:064x}"
            self.containers[cid] = {
                "Id": cid, "Name": "/" + query["name"][0], "Image": body["Image"],
                "State": "created", "Status": "Created", "Labels": body.get("Labels", {}),
                "Config": body, "HostConfig": body.get("HostConfig", {}),
                "NetworkSettings": {"Networks": {}}, "RestartCount": 0, "OOMKilled": False,
            }
            return httpx.Response(201, json={"Id": cid})
        if path.startswith("/images/"):
            image = path.removeprefix("/images/").removesuffix("/json")
            if path == "/images/create":
                self.images.add(query["fromImage"][0])
                return httpx.Response(200, content=b"{}")
            return httpx.Response(200 if image in self.images else 404, json={"message": "нет"})
        cid = path.split("/")[2]
        if cid not in self.containers:
            return httpx.Response(404, json={"message": "нет контейнера"})
        action = path.split("/")[-1]
        container = self.containers[cid]
        if request.method == "DELETE":
            del self.containers[cid]
            return httpx.Response(204)
        if action == "json":
            return httpx.Response(200, json=self._inspect(cid))
        if action == "start" or action == "restart":
            container["State"] = "running"
            if self.on_start:
                self.on_start(cid)
        elif action == "stop":
            container["State"] = "exited"
        elif action == "rename":
            container["Name"] = "/" + query["name"][0]
        elif action == "logs":
            frame = "строка\n".encode()
            return httpx.Response(
                200, content=b"\x01\0\0\0" + len(frame).to_bytes(4, "big") + frame,
            )
        return httpx.Response(204)


@pytest.fixture
def fake() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def runner_settings() -> Settings:
    return Settings(runner_token=TOKEN, runner_images="coord/agent-stub:*", image_update_timeout=2)


@pytest.fixture
def runner_app(fake: FakeDocker, runner_settings: Settings) -> Any:
    docker = DockerClient(httpx.MockTransport(fake.handle))
    return create_runner_app(runner_settings, docker)


@pytest.fixture
def runner(runner_app: Any) -> Iterator[TestClient]:
    with TestClient(runner_app, headers={"X-Runner-Token": TOKEN}) as client:
        yield client


@pytest.mark.parametrize(("image", "expected"), [
    ("coord/agent-stub:dev", True), ("coord/agent-stub:1.2.3", True),
    ("coord/agent-stub", False), ("evil/agent-stub:dev", False),
    ("coord/agent-stub:dev/../../x", True), ("alpine", False), ("", False),
])
def test_image_allowlist(image: str, expected: bool) -> None:
    assert image_allowed(image, ["coord/agent-stub:*"]) is expected


def test_env_filter_drops_dangerous_variables() -> None:
    assert filter_env({
        "AGENT_TOKEN": "x", "WORKER_NAME": "w", "STUB_MODE": "ideal", "COORD_URL": "u",
        "LD_PRELOAD": "evil.so", "PATH": "/evil", "agent_token": "x", "AGENT-X": "y",
    }) == {"AGENT_TOKEN": "x", "WORKER_NAME": "w", "STUB_MODE": "ideal", "COORD_URL": "u"}


def test_demux_logs_joins_frames() -> None:
    first, second = "привет\n".encode(), "мир\n".encode()
    raw = (
        b"\x01\0\0\0" + len(first).to_bytes(4, "big") + first
        + b"\x02\0\0\0" + len(second).to_bytes(4, "big") + second
    )
    assert demux_logs(raw) == "привет\nмир\n"


def test_demux_logs_tolerates_truncated_frame() -> None:
    assert demux_logs(b"\x01\0\0") == ""


class TestRunnerAccess:
    def test_request_without_token_is_rejected(self, runner_app: Any) -> None:
        with TestClient(runner_app) as client:
            assert client.get("/containers").status_code == 401

    def test_wrong_token_is_rejected(self, runner_app: Any) -> None:
        with TestClient(runner_app, headers={"X-Runner-Token": "wrong"}) as client:
            assert client.get("/containers").status_code == 401

    def test_empty_configured_token_never_matches(self, fake: FakeDocker) -> None:
        app = create_runner_app(
            Settings(runner_token=""), DockerClient(httpx.MockTransport(fake.handle)),
        )
        with TestClient(app, headers={"X-Runner-Token": ""}) as client:
            assert client.get("/containers").status_code == 401

    def test_only_labeled_containers_are_visible(
        self, runner: TestClient, fake: FakeDocker,
    ) -> None:
        fake.add("worker-01")
        fake.containers["db"] = {
            "Id": "db", "Name": "/coord-db-1", "Image": "postgres", "State": "running",
            "Labels": {},
        }
        body = runner.get("/containers").json()
        assert [c["worker"] for c in body] == ["worker-01"]
        assert body[0]["restart_count"] == 2

    def test_foreign_container_cannot_be_stopped(
        self, runner: TestClient, fake: FakeDocker,
    ) -> None:
        fake.containers["db"] = {
            "Id": "db", "Name": "/coord-db-1", "Image": "postgres", "State": "running",
            "Labels": {},
        }
        resp = runner.post("/containers/coord-db-1/stop")
        assert resp.status_code == 404
        assert fake.containers["db"]["State"] == "running"


class TestOperations:
    def test_stop_start_restart(self, runner: TestClient, fake: FakeDocker) -> None:
        cid = fake.add("worker-01")
        assert runner.post("/containers/worker-01/stop").status_code == 204
        assert fake.containers[cid]["State"] == "exited"
        assert runner.post("/containers/worker-01/start").status_code == 204
        assert fake.containers[cid]["State"] == "running"
        assert runner.post("/containers/worker-01/restart").status_code == 204

    def test_unknown_worker_returns_404(self, runner: TestClient) -> None:
        assert runner.post("/containers/ghost/stop").json()["code"] == "container_not_found"

    def test_logs_are_demuxed(self, runner: TestClient, fake: FakeDocker) -> None:
        fake.add("worker-01")
        assert runner.get("/containers/worker-01/logs").json()["logs"] == "строка\n"

    def test_create_uses_narrow_config(self, runner: TestClient, fake: FakeDocker) -> None:
        resp = runner.post("/containers", json={
            "name": "worker-04", "image": "coord/agent-stub:dev", "threads": 4, "cpus": 0.5,
            "memory_mb": 256,
            "env": {"AGENT_TOKEN": "t", "LD_PRELOAD": "evil", "COORD_URL": "http://api:8000"},
        })
        assert resp.status_code == 201
        created = next(iter(fake.containers.values()))
        env = dict(e.split("=", 1) for e in created["Config"]["Env"])
        assert env["AGENT_TOKEN"] == "t"
        assert env["WORKER_NAME"] == "worker-04"
        assert "LD_PRELOAD" not in env
        host = created["HostConfig"]
        assert host["Privileged"] is False
        assert host["Memory"] == 256 * 1024**2
        assert host["NanoCpus"] == 500_000_000
        assert host["Binds"] == ["lab-videos-worker-04:/data/videos", "coord-tls-ca:/certs:ro"]
        assert created["Labels"]["lab.worker"] == "worker-04"
        assert created["State"] == "running"

    @pytest.mark.parametrize("name", ["Worker", "a/b", "../etc", "-x", "a" * 64, "имя"])
    def test_create_rejects_bad_names(self, runner: TestClient, name: str) -> None:
        resp = runner.post("/containers", json={
            "name": name, "image": "coord/agent-stub:dev", "threads": 1, "cpus": 1,
            "memory_mb": 64,
        })
        assert resp.status_code == 422

    def test_create_rejects_image_outside_allowlist(
        self, runner: TestClient, fake: FakeDocker,
    ) -> None:
        resp = runner.post("/containers", json={
            "name": "worker-04", "image": "alpine:latest", "threads": 1, "cpus": 1,
            "memory_mb": 64,
        })
        assert resp.status_code == 403
        assert resp.json()["code"] == "image_not_allowed"
        assert not fake.containers

    def test_create_duplicate_is_conflict(self, runner: TestClient, fake: FakeDocker) -> None:
        fake.add("worker-04")
        resp = runner.post("/containers", json={
            "name": "worker-04", "image": "coord/agent-stub:dev", "threads": 1, "cpus": 1,
            "memory_mb": 64,
        })
        assert resp.status_code == 409

    def test_missing_image_is_pulled(self, runner: TestClient, fake: FakeDocker) -> None:
        runner.post("/containers", json={
            "name": "worker-04", "image": "coord/agent-stub:9", "threads": 1, "cpus": 1,
            "memory_mb": 64,
        })
        assert ("POST", "/images/create") in fake.calls
        assert "coord/agent-stub:9" in fake.images


class TestUpdateImage:
    def test_update_keeps_config_and_previous_until_commit(
        self, runner: TestClient, fake: FakeDocker,
    ) -> None:
        old = fake.add("worker-01")
        body = runner.post(
            "/containers/worker-01/update-image", json={"image": "coord/agent-stub:new"},
        ).json()
        new = fake.containers[body["full_id"]]
        assert new["Image"] == "coord/agent-stub:new"
        assert new["Name"] == "/coord-worker-01-1"
        assert "Hostname" not in new["Config"], "новый контейнер сообщил бы чужой id"
        assert "Cmd" not in new["Config"], "команду задаёт новый образ"
        assert "AGENT_IMAGE=coord/agent-stub:new" in new["Config"]["Env"]
        assert not any(e.startswith("PATH=") for e in new["Config"]["Env"]), "PATH задаёт образ"
        assert "AGENT_TOKEN=secret" in new["Config"]["Env"]
        assert new["Config"]["NetworkingConfig"]["EndpointsConfig"]["coord_default"]["Aliases"] == [
            "worker-01",
        ]
        assert fake.containers[old]["State"] == "exited"
        assert fake.containers[old]["Name"].endswith("-previous")

        runner.post("/updates/commit", json={"previous_id": old})
        assert old not in fake.containers

    def test_rollback_returns_previous_container(
        self, runner: TestClient, fake: FakeDocker,
    ) -> None:
        old = fake.add("worker-01")
        body = runner.post(
            "/containers/worker-01/update-image", json={"image": "coord/agent-stub:new"},
        ).json()
        runner.post("/updates/rollback", json={"full_id": body["full_id"], "previous_id": old})
        assert body["full_id"] not in fake.containers
        assert fake.containers[old]["State"] == "running"
        assert fake.containers[old]["Name"] == "/coord-worker-01-1"

    def test_failed_create_restores_old_container_immediately(
        self, runner: TestClient, fake: FakeDocker,
    ) -> None:
        old = fake.add("worker-01")
        fake.fail_create = True
        resp = runner.post(
            "/containers/worker-01/update-image", json={"image": "coord/agent-stub:new"},
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "update_failed"
        assert fake.containers[old]["State"] == "running"
        assert fake.containers[old]["Name"] == "/coord-worker-01-1"

    def test_update_rejects_image_outside_allowlist(
        self, runner: TestClient, fake: FakeDocker,
    ) -> None:
        old = fake.add("worker-01")
        resp = runner.post("/containers/worker-01/update-image", json={"image": "alpine"})
        assert resp.status_code == 403
        assert fake.containers[old]["State"] == "running"


@pytest.fixture
def api_with_runner(
    clean_db: None, runner_app: Any, runner_settings: Settings,
) -> Iterator[TestClient]:
    """API координатора, а runner подключён через ASGI: настоящая связка без сокета."""
    with TestClient(runner_app):
        client = RunnerClient(
            "http://runner", TOKEN, transport=httpx.ASGITransport(app=runner_app),
        )
        with TestClient(create_app(runner_settings, runner=client)) as api:
            api.headers["X-Agent-Token"] = FLEET_TOKEN
            login(api)
            yield api


@pytest.mark.integration
class TestApiThroughRunner:
    def test_status_and_control(self, api_with_runner: TestClient, fake: FakeDocker) -> None:
        cid = fake.add("worker-01")
        worker = register(api_with_runner, "worker-01")
        body = api_with_runner.get(f"/api/workers/{worker}/container").json()
        assert (body["state"], body["restart_count"]) == ("running", 2)
        assert api_with_runner.post(
            f"/api/workers/{worker}/container/stop",
        ).json()["state"] == "exited"
        assert fake.containers[cid]["State"] == "exited"
        assert api_with_runner.get(
            f"/api/workers/{worker}/container/logs",
        ).json()["logs"] == "строка\n"

    def test_unknown_worker_returns_404(self, api_with_runner: TestClient) -> None:
        assert api_with_runner.post("/api/workers/999/container/stop").status_code == 404

    def test_runner_rejection_keeps_code(
        self, api_with_runner: TestClient, fake: FakeDocker,
    ) -> None:
        resp = api_with_runner.post("/api/containers", json={
            "name": "worker-04", "image": "alpine", "threads": 1, "cpus": 1, "memory_mb": 64,
        })
        assert resp.status_code == 403
        assert resp.json()["code"] == "image_not_allowed"

    def test_create_issues_token_that_never_leaves_container(
        self, api_with_runner: TestClient, fake: FakeDocker,
    ) -> None:
        resp = api_with_runner.post("/api/containers", json={
            "name": "worker-04", "image": "coord/agent-stub:dev", "threads": 2, "cpus": 1,
            "memory_mb": 128,
        })
        assert resp.status_code == 201
        created = next(iter(fake.containers.values()))
        token = next(
            e.split("=", 1)[1] for e in created["Config"]["Env"] if e.startswith("AGENT_TOKEN=")
        )
        assert token not in resp.text
        # Токен привязан к имени узла.
        ok = api_with_runner.post("/api/workers/register", headers={"X-Agent-Token": token}, json={
            "name": "worker-04", "agent_version": "1", "protocol_version": 1, "threads": 2,
        })
        assert ok.status_code == 201
        other = api_with_runner.post(
            "/api/workers/register", headers={"X-Agent-Token": token},
            json={"name": "worker-05", "agent_version": "1", "protocol_version": 1, "threads": 2},
        )
        assert other.status_code == 403

    def test_update_commits_when_new_container_registers(
        self, api_with_runner: TestClient, fake: FakeDocker,
    ) -> None:
        old = fake.add("worker-01")
        worker = register(api_with_runner, "worker-01")

        # Новый контейнер запустился и зарегистрировался со своим идентификатором.
        fake.on_start = lambda cid: sql("UPDATE workers SET container_id = :c", c=cid[:12])

        resp = api_with_runner.post(
            f"/api/workers/{worker}/container/update-image",
            json={"image": "coord/agent-stub:new"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["image"] == "coord/agent-stub:new"
        assert old not in fake.containers

    def test_update_rolls_back_when_agent_does_not_register(
        self, api_with_runner: TestClient, fake: FakeDocker,
    ) -> None:
        old = fake.add("worker-01")
        worker = register(api_with_runner, "worker-01")
        resp = api_with_runner.post(
            f"/api/workers/{worker}/container/update-image",
            json={"image": "coord/agent-stub:new"},
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "update_rolled_back"
        assert fake.containers[old]["State"] == "running"
        assert len(fake.containers) == 1


@pytest.mark.integration
class TestMinimalMode:
    def test_without_runner_control_returns_503(self, client: TestClient) -> None:
        worker = register(client)
        resp = client.post(f"/api/workers/{worker}/container/stop")
        assert resp.status_code == 503
        assert resp.json()["code"] == "runner_unavailable"

    def test_unreachable_runner_returns_503(self, clean_db: None) -> None:
        settings = Settings(runner_url="http://127.0.0.1:1", runner_token="x")
        with TestClient(create_app(settings)) as api:
            login(api)
            assert api.get("/api/containers").status_code == 503


def sync(containers: list[dict[str, Any]]) -> None:
    async def run() -> None:
        settings = Settings()
        engine = db.make_engine(settings.database_url)
        try:
            async with db.make_sessionmaker(engine)() as session:
                await sync_containers(session, containers)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.integration
class TestStateSync:
    CONTAINER = {"worker": "worker-01", "id": "abc", "image": "i", "state": "running",
                 "restart_count": 3, "oom_killed": True}

    def test_state_is_stored(self, client: TestClient) -> None:
        worker = register(client)
        sync([self.CONTAINER])
        card = client.get(f"/api/workers/{worker}").json()
        assert card["status"] == "online"
        assert card["container_state"]["restart_count"] == 3
        assert card["container_state"]["oom_killed"] is True

    def test_stopped_container_frees_chunk_at_once(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        sync([{**self.CONTAINER, "state": "exited"}])
        run_dispatcher_tick()
        assert client.get(f"/api/workers/{worker}").json()["status"] == "offline"
        again = must_lease(client, register(client, "other"))
        assert again["chunk_id"] == chunk["chunk_id"]

    def test_vanished_container_is_marked_missing(self, client: TestClient) -> None:
        worker = register(client)
        sync([self.CONTAINER])
        sync([])
        card = client.get(f"/api/workers/{worker}").json()
        assert card["container_state"]["state"] == "missing"
        assert card["status"] == "offline"

    def test_worker_outside_runner_is_not_touched(self, client: TestClient) -> None:
        worker = register(client, "external")
        sync([self.CONTAINER])
        card = client.get(f"/api/workers/{worker}").json()
        assert card["status"] == "online"
        assert card["container_state"] is None


class TestSshRoutes:
    @pytest.mark.parametrize("path", [
        "/ssh/status", "/ssh/start", "/ssh/stop", "/ssh/restart", "/ssh/rollback", "/ssh/logs",
        "/ssh/deploy", "/ssh/forget-host-key", "/ssh/servers",
    ])
    def test_every_route_requires_token(self, runner_app: Any, path: str) -> None:
        with TestClient(runner_app, headers={"X-Runner-Token": "wrong"}) as client:
            assert client.post(path, json={}).status_code == 401


class TestServerContainer:
    """Контейнер, который изображает сервер: в нём нет ни сокета Docker, ни закрытого ключа."""

    @pytest.fixture
    def servers(self, fake: FakeDocker, tmp_path: Path) -> Iterator[TestClient]:
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(str(tmp_path / "ssh_client_key"))
        manager = SshManager(tmp_path, tmp_path / "pins.json", tmp_path, ["lab-*"])
        app = create_runner_app(
            Settings(runner_token=TOKEN), DockerClient(httpx.MockTransport(fake.handle)), manager,
        )
        with TestClient(app, headers={"X-Runner-Token": TOKEN}) as client:
            yield client

    def create(self, client: TestClient, **fields: Any) -> httpx.Response:
        body = {"name": "worker-04", "cpus": 0.5, "memory_mb": 256, "key_ref": "ssh_client_key"}
        return client.post("/ssh/servers", json=body | fields)

    def test_created_and_started_without_secrets(
        self, servers: TestClient, fake: FakeDocker,
    ) -> None:
        response = self.create(servers)
        assert response.status_code == 201
        assert response.json()["host"] == "lab-worker-04"
        container = next(iter(fake.containers.values()))
        assert container["State"] == "running"
        config = container["Config"]
        host = config["HostConfig"]
        assert host["Privileged"] is False
        assert host["Init"] is True
        assert host["NanoCpus"] == 500_000_000
        assert host["Memory"] == 256 * 1024**2
        assert not any("docker.sock" in bind for bind in host["Binds"])
        env = " ".join(config["Env"])
        assert "SSH_AUTHORIZED_KEY=ssh-ed25519 " in env
        assert "PRIVATE KEY" not in json.dumps(config)

    def test_second_server_with_same_name_is_refused(self, servers: TestClient) -> None:
        assert self.create(servers).status_code == 201
        assert self.create(servers).status_code == 409

    def test_bad_name_and_unknown_key_are_refused(
        self, servers: TestClient, fake: FakeDocker,
    ) -> None:
        assert self.create(servers, name="../evil").status_code == 422
        assert self.create(servers, key_ref="no_such_key").status_code == 422
        assert not fake.containers
