#!/usr/bin/env python3
"""Сценарии отказов на поднятом стенде (ТЗ, п. 13 и 20): проверяет то, что нельзя проверить
в pytest, потому что нужны настоящие docker kill и docker stop.

Запуск: python scripts/chaos.py [сценарий ...]. Только стандартная библиотека.
Часть сценариев управляет агентом через файл заглушки, поэтому стенд нужен с заглушками:
    COORD_AGENT_KIND=stub docker compose up -d --build
Базовый адрес: COORD_URL, по умолчанию https://localhost:8443 с проверкой по deploy/tls/ca.crt.
Вход под администратором, пароль: COORD_ADMIN_PASSWORD или файл secrets/coord_admin_password.
"""
import http.cookiejar
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

BASE = os.environ.get("COORD_URL", "https://localhost:8443").rstrip("/")
COMPOSE = os.environ.get("COMPOSE_CMD", "docker compose").split()
HEADERS: dict[str, str] = {}
OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    urllib.request.HTTPSHandler(
        context=ssl.create_default_context(cafile=os.environ.get("COORD_CA", "deploy/tls/ca.crt")),
    ),
)


def api(method: str, path: str, payload: Any = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **HEADERS},
    )
    with OPENER.open(request, timeout=15) as response:
        body = response.read()
        return json.loads(body) if body else None


def admin_password() -> str:
    if "COORD_ADMIN_PASSWORD" in os.environ:
        return os.environ["COORD_ADMIN_PASSWORD"]
    with open("secrets/coord_admin_password", encoding="utf-8") as secret:
        return secret.read().strip()


def login() -> bool:
    me = api("POST", "/api/auth/login", {"login": "admin", "password": admin_password()})
    HEADERS["X-CSRF-Token"] = me["csrf_token"]
    return True


def compose(*args: str) -> str:
    done = subprocess.run([*COMPOSE, *args], capture_output=True, text=True, check=True)
    return done.stdout


def wait_for(what: str, condition: Callable[[], bool], timeout: float = 90.0) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if condition():
                return time.monotonic() - started
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(0.5)
    raise AssertionError(f"не дождались за {timeout:.0f} с: {what}")


def new_job(text: str, chunk_size: int = 500, **fields: Any) -> int:
    job = api("POST", "/api/jobs", {"range": text, "chunk_size": chunk_size, **fields})
    api("POST", f"/api/jobs/{job['id']}/start")
    return job["id"]


def job(job_id: int) -> dict[str, Any]:
    return api("GET", f"/api/jobs/{job_id}")


def workers() -> dict[str, dict[str, Any]]:
    return {w["name"]: w for w in api("GET", "/api/workers")}


def control(service: str, values: str) -> None:
    """Управляющий файл заглушки на сервере. От имени агента: иначе он не сможет его удалить."""
    compose("exec", "-T", "--user", "agent", service, "sh", "-c",
            f"echo '{values}' > /tmp/stub-control.json")


def clear_control(service: str) -> None:
    compose("exec", "-T", "--user", "agent", service, "rm", "-f", "/tmp/stub-control.json")


def restarts(name: str) -> int:
    """Сколько раз координатор поднимал упавшего агента по SSH."""
    return int((workers()[name]["container_state"] or {}).get("restart_count") or 0)


def wait_online(*names: str) -> None:
    wait_for(f"узлы в сети: {names}", lambda: all(
        workers().get(n, {}).get("status") == "online" for n in names
    ))


def finish(job_id: int, timeout: float = 240.0) -> dict[str, Any]:
    wait_for(f"завершение задания {job_id}", lambda: job(job_id)["status"] in (
        "completed", "failed", "stopped",
    ), timeout)
    return job(job_id)


def check_complete(job_id: int, total: int) -> None:
    body = finish(job_id)
    assert body["status"] == "completed", body
    assert body["processed_ids"] == total, body
    assert sum(body["items"].values()) == total, f"дубликаты или потери: {body['items']}"


def scenario_kill_worker() -> str:
    """docker kill узла: блок уходит другому, задание доходит до конца, дубликатов нет."""
    wait_online("worker-01", "worker-02", "worker-03")
    job_id = new_job("[1,20000]", 1000)
    wait_for("узел взял блок", lambda: workers()["worker-02"]["leased_chunks"] > 0)
    compose("kill", "worker-02")
    started = time.monotonic()
    wait_for("блок снят с упавшего узла", lambda: workers()["worker-02"]["leased_chunks"] == 0)
    reclaimed = time.monotonic() - started
    check_complete(job_id, 20000)
    compose("up", "-d", "worker-02")
    wait_online("worker-02")
    return f"блок снят через {reclaimed:.1f} с, задание завершено, дубликатов нет"


def scenario_stop_worker() -> str:
    """docker stop узла: то же самое, контейнер завершается быстро."""
    wait_online("worker-01")
    job_id = new_job("[1,10000]", 500)
    wait_for("узел взял блок", lambda: workers()["worker-01"]["leased_chunks"] > 0)
    started = time.monotonic()
    compose("stop", "worker-01")
    stop_took = time.monotonic() - started
    assert stop_took < 8, f"docker stop занял {stop_took:.1f} с"
    check_complete(job_id, 10000)
    compose("start", "worker-01")
    wait_online("worker-01")
    return f"docker stop за {stop_took:.1f} с, задание завершено"


def scenario_crash_by_command() -> str:
    """Агент упал на сервере: координатор сам поднимает его по SSH, работа идёт с next_id."""
    wait_online("worker-03")
    before = restarts("worker-03")
    job_id = new_job("[1,6000]", 300)
    wait_for("узел взял блок", lambda: workers()["worker-03"]["leased_chunks"] > 0)
    control("worker-03", '{"crash": true}')
    wait_for("агент поднят по SSH", lambda: restarts("worker-03") > before)
    check_complete(job_id, 6000)
    wait_online("worker-03")
    return f"перезапусков агента: {restarts('worker-03')}, задание завершено"


def scenario_restart_api() -> str:
    """Перезапуск координатора: подтверждённое не теряется, задание продолжается."""
    wait_online("worker-01", "worker-02", "worker-03")
    job_id = new_job("[1,30000]", 1000)
    wait_for("есть прогресс", lambda: job(job_id)["processed_ids"] > 2000)
    processed = job(job_id)["processed_ids"]
    compose("kill", "api")
    time.sleep(3)
    compose("up", "-d", "api")
    wait_for("api снова отвечает", lambda: job(job_id)["id"] == job_id)
    assert job(job_id)["processed_ids"] >= processed, "подтверждённый прогресс потерян"
    check_complete(job_id, 30000)
    return f"до перезапуска {processed} идентификаторов, потерь нет"


def scenario_restart_dispatcher() -> str:
    """Перезапуск диспетчера не мешает выдаче: она идёт через API, таймеры наверстают."""
    wait_online("worker-01")
    job_id = new_job("[1,10000]", 500)
    compose("kill", "dispatcher")
    wait_for("есть прогресс без диспетчера", lambda: job(job_id)["processed_ids"] > 1000)
    compose("up", "-d", "dispatcher")
    check_complete(job_id, 10000)
    return "выдача не останавливалась"


def scenario_restart_database() -> str:
    """Перезапуск базы: агенты получают 503, повторяют, задание завершается без потерь."""
    wait_online("worker-01", "worker-02", "worker-03")
    job_id = new_job("[1,20000]", 1000)
    wait_for("есть прогресс", lambda: job(job_id)["processed_ids"] > 1000)
    compose("restart", "db")
    wait_for("api снова отвечает", lambda: job(job_id)["id"] == job_id)
    check_complete(job_id, 20000)
    return "задание завершено после перезапуска базы"


def scenario_low_disk() -> str:
    """Нехватка места: узел перестаёт получать блоки, остальные работают."""
    wait_online("worker-01", "worker-02")
    control("worker-01", '{"disk_free_bytes": 1000}')
    try:
        wait_for("узел сообщил о нехватке места",
                 lambda: workers()["worker-01"]["metrics"].get("disk_free_bytes") == 1000)
        job_id = new_job("[1,10000]", 500)
        wait_for("причина отказа видна",
                 lambda: workers()["worker-01"]["lease_block_reason"] == "disk_low")
        assert workers()["worker-01"]["leased_chunks"] == 0
        check_complete(job_id, 10000)
    finally:
        clear_control("worker-01")
    wait_for("узел снова получает блоки", lambda: (
        new_job("[1,2000]", 100) and workers()["worker-01"]["lease_block_reason"] is None
    ))
    return "узел с нехваткой места не получал блоков"


def scenario_rate_limited() -> str:
    """429 от внешнего сервиса снижает общую частоту запросов."""
    wait_online("worker-01", "worker-02", "worker-03")
    base = api("GET", "/api/rate-limit")["base_rps"]
    control("worker-02", '{"rate_limited": true}')
    try:
        job_id = new_job("[1,5000]", 200)
        wait_for("лимит снизился", lambda: (
            (api("GET", "/api/rate-limit")["effective_rps"] or base) < base
        ))
        low = api("GET", "/api/rate-limit")["effective_rps"]
    finally:
        clear_control("worker-02")
    api("POST", f"/api/jobs/{job_id}/stop")
    wait_for("лимит восстановился", lambda: (
        (api("GET", "/api/rate-limit")["effective_rps"] or 0) >= base * 0.9
    ), 120)
    return f"лимит снизился с {base} до {low} запросов/с и восстановился"


def scenario_recreate_new_ip() -> str:
    """Сервер пересоздан начисто: координатор сам заново ставит агента, узел тот же."""
    wait_online("worker-03")
    before = workers()["worker-03"]

    def deploys() -> int:
        return len(api("GET", f"/api/events?operation=agent.deploy&worker_id={before['id']}"))

    installed = deploys()
    job_id = new_job("[1,8000]", 400)
    wait_for("узел взял блок", lambda: workers()["worker-03"]["leased_chunks"] > 0)
    compose("up", "-d", "--force-recreate", "--no-deps", "worker-03")
    wait_for("агент установлен заново", lambda: deploys() > installed)
    wait_online("worker-03")
    after = workers()["worker-03"]
    assert after["id"] == before["id"], "узел должен сохранить идентификатор"
    check_complete(job_id, 8000)
    return f"id {after['id']} сохранён, агент установлен заново, адрес {after['last_ip']}"


def scenario_network_loss() -> str:
    """Обрыв сети узла: блок уходит другим, после возврата сети узел работает дальше."""
    wait_online("worker-01", "worker-02")
    container = compose("ps", "-q", "worker-01").strip()
    job_id = new_job("[1,25000]", 500)
    wait_for("узел взял блок", lambda: workers()["worker-01"]["leased_chunks"] > 0)
    subprocess.run(["docker", "network", "disconnect", "coord_default", container],
                   check=True, capture_output=True)
    try:
        started = time.monotonic()
        wait_for("узел признан недоступным", lambda: workers()["worker-01"]["status"] == "offline")
        wait_for("блок снят", lambda: workers()["worker-01"]["leased_chunks"] == 0)
        noticed = time.monotonic() - started
    finally:
        subprocess.run(["docker", "network", "connect", "coord_default", container],
                       check=True, capture_output=True)
    check_complete(job_id, 25000)
    wait_online("worker-01")
    return f"блок снят через {noticed:.1f} с, узел вернулся, дубликатов нет"


def scenario_oom_kill() -> str:
    """Настоящий OOM-kill по лимиту сервера: ядро убивает агента, координатор поднимает его."""
    wait_online("worker-03")
    before = restarts("worker-03")
    job_id = new_job("[1,6000]", 300)
    wait_for("узел взял блок", lambda: workers()["worker-03"]["leased_chunks"] > 0)
    # Docker разрешает своп размером в лимит памяти: всего 512 МБ при лимите 256 МБ.
    control("worker-03", '{"allocate_mb": 800}')
    wait_for("агент убит и поднят по SSH", lambda: restarts("worker-03") > before)
    check_complete(job_id, 6000)
    wait_online("worker-03")
    return f"перезапусков стало {restarts('worker-03')}, дубликатов нет"


def scenario_real_disk_full() -> str:
    """Настоящее заполнение диска: tmpfs на 2 МБ, файлы по 256 КБ. Узел перестаёт получать блоки."""
    wait_online("worker-01")
    name = "chaos-disk-full"
    token = api("POST", "/api/agent-tokens", {"worker_name": name})
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "build", "-q", "-t", "coord/agent-stub:dev", "stub"],
                   check=True, capture_output=True)
    subprocess.run([
        "docker", "run", "-d", "--name", name, "--network", "coord_default",
        "--tmpfs", "/data/videos:size=2m,uid=10001", "-v", "coord-tls-ca:/certs:ro",
        "-e", f"WORKER_NAME={name}", "-e", f"AGENT_TOKEN={token['token']}",
        "-e", "COORD_URL=https://web", "-e", "SSL_CERT_FILE=/certs/ca.crt",
        "-e", "WORKER_THREADS=4", "-e", "STUB_FILE_SIZE=262144", "-e", "STUB_ITEM_DELAY=0.02",
        "coord/agent-stub:dev",
    ], check=True, capture_output=True)
    try:
        wait_online(name)
        node = workers()[name]
        # Резерв 1 МБ: узел успевает заполнить диск и потом перестаёт получать блоки.
        api("PATCH", f"/api/workers/{node['id']}", {"min_free_space_gb": 0.001})
        job_id = new_job("[1,8000]", 100)
        wait_for("узел перестал получать блоки",
                 lambda: workers()[name]["lease_block_reason"] == "disk_low")
        free = workers()[name]["metrics"]["disk_free_bytes"]
        body = finish(job_id)
        assert body["status"] == "completed", body
        assert sum(body["items"].values()) == 8000, body["items"]
        return f"свободно {free} байт, блоков не получает, задание закончили другие узлы"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        api("POST", f"/api/agent-tokens/{token['id']}/revoke")
        api("PATCH", f"/api/workers/{workers()[name]['id']}", {"enabled": False})


SCENARIOS = {
    name.removeprefix("scenario_"): fn
    for name, fn in globals().items() if name.startswith("scenario_")
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    wait_for("вход в систему", login)
    names = sys.argv[1:] or list(SCENARIOS)
    failed = 0
    for name in names:
        started = time.monotonic()
        try:
            # Предыдущий сценарий мог провалиться, не вернув контейнеры.
            compose("up", "-d")
            note = SCENARIOS[name]()
            print(f"OK    {name:22} {time.monotonic() - started:6.1f} с  {note}", flush=True)
        except Exception as exc:  # отчёт должен дойти до конца даже при провале сценария
            failed += 1
            print(f"FAIL  {name:22} {time.monotonic() - started:6.1f} с  {exc}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
