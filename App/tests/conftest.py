import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import Connection, create_engine, text

from coordinator.api.app import create_app
from coordinator.config import Settings
from coordinator.domain.security import Role, hash_password
from coordinator.storage.tokens import hash_token

FLEET_TOKEN = "test-fleet-token"
ADMIN_LOGIN = "admin"
PASSWORD = "test-password-1"
# scrypt намеренно медленный: один хэш на все тесты.
PASSWORD_HASH = hash_password(PASSWORD)


def seed_user(conn: Connection, name: str, role: Role) -> None:
    conn.execute(text("""
        WITH created AS (
            INSERT INTO users (login, password_hash) VALUES (:login, :hash) RETURNING id
        )
        INSERT INTO user_roles (user_id, role_id)
        SELECT created.id, roles.id FROM created, roles WHERE roles.name = :role
    """), {"login": name, "hash": PASSWORD_HASH, "role": role.value})


def login(client: Any, name: str = ADMIN_LOGIN, password: str = PASSWORD) -> None:
    """Входит и настраивает клиент как браузер: cookie сессии и заголовок CSRF."""
    resp = client.post("/api/auth/login", json={"login": name, "password": password})
    assert resp.status_code == 200, resp.text
    client.headers["X-CSRF-Token"] = resp.json()["csrf_token"]


@pytest.fixture
def clean_db() -> None:
    engine = create_engine(Settings().database_url)
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE items, range_chunks, jobs, workers, agent_tokens, idempotency_keys, "
            "worker_metrics, users, events, audit_log, job_samples RESTART IDENTITY CASCADE"
        ))
        seed_user(conn, ADMIN_LOGIN, Role.ADMINISTRATOR)
        conn.execute(
            text("INSERT INTO agent_tokens (token_hash) VALUES (:token_hash)"),
            {"token_hash": hash_token(FLEET_TOKEN)},
        )
    engine.dispose()
    with Redis.from_url(Settings().redis_url) as cache:
        cache.flushdb()


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    # Квота блоков равна числу потоков: тестам удобно брать несколько блоков одним узлом.
    with TestClient(create_app(Settings(threads_per_chunk=1))) as test_client:
        test_client.headers["X-Agent-Token"] = FLEET_TOKEN
        login(test_client)
        yield test_client


@pytest.fixture
def live_url(clean_db: None) -> Iterator[str]:
    """Настоящий HTTP-сервер: заглушка и клиент агента ходят по сети, а не через TestClient."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(Settings()), host="127.0.0.1", port=port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
