#!/usr/bin/env python3
"""Чек-лист раздела 16 плана «подключили агента — всё работает».

Выполняется дважды: с заглушками и с настоящими агентами. Второй прогон не требует правок платформы.

    python scripts/acceptance.py            заглушки, стенд из compose.yaml
    python scripts/acceptance.py --real     настоящие агенты, deploy/compose.real-agent.yaml
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import chaos  # noqa: E402

REAL = "--real" in sys.argv
if REAL:
    chaos.COMPOSE = "docker compose -f compose.yaml -f deploy/compose.real-agent.yaml".split()
# Настоящие агенты ходят на тестовый сайт с задержками 429, диапазон меньше.
TOTAL = 1200 if REAL else 60000
CHUNK = 100 if REAL else 1000
RANGE = f"[100,{99 + TOTAL}]" if REAL else f"[1,{TOTAL}]"
NAMES = ("worker-01", "worker-02", "worker-03")

passed = 0


def step(number: int, text: str, note: str = "") -> None:
    global passed
    passed += 1
    print(f"OK {number:>2}. {text}" + (f" ({note})" if note else ""), flush=True)


def agent_post(path: str, token: str, payload: dict) -> int:
    request = urllib.request.Request(
        chaos.BASE + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Agent-Token": token},
    )
    context = ssl.create_default_context(cafile="deploy/tls/ca.crt")
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    chaos.wait_for("вход в систему", chaos.login, 180)

    health = chaos.compose("exec", "-T", "api", "python", "-c", (
        "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read())"
    ))
    assert '"status":"ok"' in health, health
    step(1, "Стенд поднят, база мигрирована, координатор отвечает на /health")

    chaos.wait_online(*NAMES)
    nodes = chaos.workers()
    threads = {n: nodes[n]["threads"] for n in NAMES}
    limits = {nodes[n]["cpu_limit"] for n in NAMES}
    assert len(set(threads.values())) == 3 and len(limits) == 3, (threads, limits)
    version = nodes["worker-01"]["agent_version"]
    step(2, "Три контейнера агента с разными лимитами и потоками", f"агент {version}, {threads}")

    job_id = chaos.new_job(RANGE, CHUNK)
    step(3, "Создано задание на конечном диапазоне, запущено", f"№ {job_id}, {RANGE}")

    chaos.wait_for("прогресс растёт", lambda: chaos.job(job_id)["processed_ids"] > 0, 60)
    first = chaos.job(job_id)["processed_ids"]
    chaos.wait_for("прогресс растёт дальше", lambda: chaos.job(job_id)["processed_ids"] > first, 60)
    step(4, "Блоки выдаются по мере освобождения узлов, прогресс растёт")

    chaos.wait_for("есть результаты с файлом", lambda: bool(_items_with_files(job_id)), 120)
    item = _items_with_files(job_id)[0]
    assert item["title"] and item["checksum"], item
    chaos.compose("exec", "-T", item["worker"], "test", "-s", item["file_path"])
    step(5, "Элементы получают метаданные, файлы появляются в томах",
         f"{item['external_id']}: {item['file_path']}")

    # Узел с блоком останавливается, блок возвращается и уходит другому.
    chaos.wait_for(
        "блок у worker-02", lambda: chaos.workers()["worker-02"]["leased_chunks"] > 0, 60,
    )
    chaos.compose("stop", "worker-02")
    chaos.wait_for("блок снят", lambda: chaos.workers()["worker-02"]["leased_chunks"] == 0, 60)
    step(6, "Контейнер остановлен: его блок вернулся в очередь")

    chaos.compose("start", "worker-02")
    chaos.wait_online("worker-02")
    step(7, "Контейнер запущен снова и продолжает работу")

    base = chaos.api("GET", "/api/rate-limit")["base_rps"]
    if not REAL:
        chaos.compose("exec", "-T", "worker-02", "sh", "-c",
                      'echo \'{"rate_limited": true}\' > /tmp/stub-control.json')
    chaos.wait_for("общий лимит снизился", lambda: (
        (chaos.api("GET", "/api/rate-limit")["effective_rps"] or base) < base
    ), 60)
    low = chaos.api("GET", "/api/rate-limit")["effective_rps"]
    if not REAL:
        chaos.compose("exec", "-T", "worker-02", "rm", "-f", "/tmp/stub-control.json")
    step(8, "Ответы 429: суммарная частота запросов снижается", f"{base} → {low} запросов/с")

    victim = chaos.workers()["worker-01"]["id"]
    chaos.api("PATCH", f"/api/workers/{victim}", {"min_free_space_gb": 1_000_000})
    chaos.wait_for("узел перестал получать блоки", lambda: (
        chaos.workers()["worker-01"]["lease_block_reason"] == "disk_low"
    ), 60)
    chaos.api("PATCH", f"/api/workers/{victim}", {"min_free_space_gb": None})
    step(9, "Нехватка места: узел перестаёт получать блоки")

    done_before = chaos.job(job_id)["processed_ids"]
    chaos.compose("kill", "api")
    time.sleep(2)
    chaos.compose("up", "-d", "api")
    chaos.wait_for("api снова отвечает", lambda: chaos.job(job_id)["id"] == job_id, 90)
    assert chaos.job(job_id)["processed_ids"] >= done_before, "прогресс потерян"
    step(10, "Координатор перезапущен: состояние восстановлено, прогресс не потерян",
         f"было {done_before}")

    body = chaos.finish(job_id, 480)
    assert body["status"] == "completed", body
    assert body["processed_ids"] == TOTAL, body
    assert sum(body["items"].values()) == TOTAL, body["items"]
    events = chaos.api("GET", f"/api/jobs/{job_id}/events?limit=500")
    assert {e["operation"] for e in events} >= {"chunk.lease", "chunk.complete"}, events[:3]
    report = chaos.OPENER.open(chaos.BASE + f"/api/jobs/{job_id}/items.csv", timeout=30).read()
    assert report.count(b"\n") >= TOTAL, "экспорт неполный"
    step(11, "Задание завершено, отчёт и журнал событий доступны",
         f"{dict(filter(lambda kv: kv[1], body['items'].items()))}")

    done = next(e for e in chaos.api("GET", f"/api/events?operation=chunk.complete&job_id={job_id}"
                                            f"&worker_id={victim}&result=done&limit=1"))
    token = Path("secrets/worker_01_token").read_text().strip()
    code = agent_post(f"/api/workers/{victim}/complete", token, {
        "chunk_id": done["chunk_id"], "next_id": 1, "results": [],
    })
    assert code == 200, code
    assert sum(chaos.job(job_id)["items"].values()) == TOTAL
    step(12, "Повторная отправка того же результата не создаёт дубликата")

    print(f"Пройдено шагов: {passed} из 12", flush=True)
    return 0


def _items_with_files(job_id: int) -> list[dict]:
    page = chaos.api("GET", f"/api/jobs/{job_id}/items?status=completed&limit=5")
    return [i for i in page["items"] if i["file_path"]]


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    raise SystemExit(main())
