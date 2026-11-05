#!/usr/bin/env python3
"""Нагрузочный тест координатора (ТЗ, п. 20): десятки агентов, сотни потоков, массовые «не найдено».

Запускается в контейнере на сети стенда, агенты идут тем же путём, что настоящие: через nginx и TLS.
Сайт не нужен: агент здесь сразу придумывает результат, поэтому видно предел самого координатора.

    scripts/loadtest.sh [--only сценарий]
"""
import argparse
import asyncio
import json
import os
import ssl
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("COORD_URL", "https://web")
CA_FILE = os.environ.get("COORD_CA", "/certs/ca.crt")
PASSWORD_FILE = "/run/secrets/coord_admin_password"
HEARTBEAT_S = 5.0


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    agents: int
    threads: int
    total_ids: int
    chunk_size: int
    batch: int = 100
    # Секунд на пакет: имитация медленных ответов сайта.
    batch_delay: float = 0.0
    # Доля найденных материалов с метаданными и файлами.
    found_every: int = 0


SCENARIOS = [
    Scenario("mass_not_found", "Большой диапазон, почти всё «не найдено»",
             agents=30, threads=8, total_ids=300_000, chunk_size=1000),
    Scenario("many_threads", "60 агентов, 960 потоков суммарно",
             agents=60, threads=16, total_ids=240_000, chunk_size=1000),
    Scenario("slow_site", "Медленные ответы сайта: агент долго держит блок",
             agents=40, threads=8, total_ids=40_000, chunk_size=500, batch_delay=0.5),
    Scenario("found_with_files", "Каждый пятый найден, с метаданными и файлом",
             agents=20, threads=8, total_ids=60_000, chunk_size=1000, found_every=5),
    Scenario("lease_contention", "Очень мелкие блоки: упор в выдачу и закрытие",
             agents=50, threads=4, total_ids=20_000, chunk_size=10, batch=10),
]


@dataclass
class Stats:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: Counter = field(default_factory=Counter)
    failures: Counter = field(default_factory=Counter)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


class Agent:
    def __init__(
        self, index: int, scenario: Scenario, http: httpx.AsyncClient, token: str, stats: Stats,
    ) -> None:
        self.name = f"load-{scenario.name}-{index:03d}"
        self.scenario = scenario
        self.http = http
        self.token = token
        self.stats = stats
        self.worker_id: int | None = None
        self.checked = 0
        self.active: int | None = None

    async def call(self, op: str, path: str, payload: dict[str, Any]) -> httpx.Response | None:
        started = time.perf_counter()
        try:
            resp = await self.http.post(
                path, json=payload,
                headers={"X-Agent-Token": self.token, "X-Protocol-Version": "1"},
            )
        except httpx.HTTPError as exc:
            self.stats.failures[f"{op}: {type(exc).__name__}"] += 1
            return None
        self.stats.latencies[op].append(time.perf_counter() - started)
        self.stats.statuses[resp.status_code] += 1
        if resp.status_code >= 400:
            self.stats.failures[f"{op}: HTTP {resp.status_code}"] += 1
        return resp

    def result(self, item: int) -> dict[str, Any]:
        every = self.scenario.found_every
        if every and item % every == 0:
            return {
                "external_id": item, "status": "completed", "http_status": 200,
                "title": f"Материал {item}", "page_url": f"http://load.invalid/{item}",
                "download_url": f"http://load.invalid/{item}/video.mp4",
                "file": {"path": f"/data/videos/{str(item)[:3]}/{item}/video.mp4",
                         "size": 1_000_000, "sha256": f"{item:064x}", "mime": "video/mp4"},
            }
        return {"external_id": item, "status": "not_found", "http_status": 404}

    async def heartbeat_loop(self, done: asyncio.Event) -> None:
        while not done.is_set():
            await asyncio.sleep(HEARTBEAT_S)
            await self.call("heartbeat", f"/api/workers/{self.worker_id}/heartbeat", {
                "status": "busy" if self.active else "idle",
                "free_slots": 0 if self.active else self.scenario.threads,
                "active_chunks": [self.active] if self.active else [],
                "metrics": {"cpu_cores": 0.1, "cpu_limit": 1.0, "memory_bytes": 10**8,
                            "memory_limit": 10**9, "disk_free_bytes": 10**12},
                "counters": {"checked": self.checked, "failed": 0, "rate_limited": 0},
            })

    async def work(self, done: asyncio.Event) -> None:
        resp = await self.call("register", "/api/workers/register", {
            "name": self.name, "agent_version": "load-1", "protocol_version": 1,
            "threads": self.scenario.threads, "storage_path": "/data/videos",
            "limits": {"cpus": 16.0, "memory_mb": 1024},
        })
        if resp is None or resp.status_code != 201:
            return
        self.worker_id = resp.json()["worker_id"]
        beat = asyncio.create_task(self.heartbeat_loop(done))
        base = f"/api/workers/{self.worker_id}"
        try:
            while not done.is_set():
                resp = await self.call("lease", f"{base}/lease", {
                    "free_slots": self.scenario.threads,
                })
                if resp is None or resp.status_code == 204:
                    await asyncio.sleep(0.2)
                    continue
                if resp.status_code != 200:
                    await asyncio.sleep(1)
                    continue
                await self.process(base, resp.json())
        finally:
            beat.cancel()

    async def process(self, base: str, chunk: dict[str, Any]) -> None:
        chunk_id, end = chunk["chunk_id"], chunk["range_end"]
        next_id = chunk["next_id"]
        self.active = chunk_id
        while next_id <= end:
            upto = min(next_id + self.scenario.batch - 1, end)
            results = [self.result(i) for i in range(next_id, upto + 1)]
            if self.scenario.batch_delay:
                await asyncio.sleep(self.scenario.batch_delay)
            self.checked += len(results)
            next_id = upto + 1
            if next_id > end:
                await self.call("complete", f"{base}/complete", {
                    "chunk_id": chunk_id, "next_id": next_id, "results": results,
                })
            else:
                await self.call("progress", f"{base}/progress", {
                    "chunk_id": chunk_id, "next_id": next_id, "results": results,
                })
        self.active = None


async def run_scenario(
    scenario: Scenario, admin: httpx.AsyncClient, http: httpx.AsyncClient, token: str,
) -> dict[str, Any]:
    stats = Stats()
    job = (await admin.post("/api/jobs", json={
        "range": f"[1,{scenario.total_ids}]", "chunk_size": scenario.chunk_size,
    })).json()
    done = asyncio.Event()
    agents = [Agent(i, scenario, http, token, stats) for i in range(scenario.agents)]
    tasks = [asyncio.create_task(a.work(done)) for a in agents]
    await asyncio.sleep(1)
    await admin.post(f"/api/jobs/{job['id']}/start")
    started = time.perf_counter()
    ui_latencies: list[float] = []
    state: dict[str, Any] = {}
    while time.perf_counter() - started < 900:
        before = time.perf_counter()
        state = (await admin.get(f"/api/jobs/{job['id']}")).json()
        await admin.get("/api/dashboard")
        ui_latencies.append(time.perf_counter() - before)
        if state["status"] in ("completed", "failed", "stopped"):
            break
        await asyncio.sleep(2)
    elapsed = time.perf_counter() - started
    done.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    for agent in agents:
        if agent.worker_id:
            await admin.patch(f"/api/workers/{agent.worker_id}", json={"enabled": False})
    state = (await admin.get(f"/api/jobs/{job['id']}")).json()
    return {
        "scenario": scenario, "job": state, "seconds": elapsed,
        "ids_per_second": state["processed_ids"] / elapsed,
        "requests": sum(stats.statuses.values()),
        "requests_per_second": sum(stats.statuses.values()) / elapsed,
        "latency": {
            op: {
                "count": len(values), "p50": percentile(values, 0.5),
                "p95": percentile(values, 0.95), "p99": percentile(values, 0.99),
                "max": max(values), "mean": statistics.fmean(values),
            }
            for op, values in sorted(stats.latencies.items())
        },
        "ui_p95": percentile(ui_latencies, 0.95),
        "statuses": dict(stats.statuses),
        "failures": dict(stats.failures),
    }


def ms(value: float) -> str:
    return f"{value * 1000:.0f}"


def report(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        s: Scenario = r["scenario"]
        job = r["job"]
        total_items = sum(job["items"].values())
        lines += [
            f"### {s.name}: {s.description}", "",
            f"Агентов: {s.agents}, потоков у каждого: {s.threads} "
            f"(всего {s.agents * s.threads}), идентификаторов: {s.total_ids}, "
            f"размер блока: {s.chunk_size}, задержка на пакет: {s.batch_delay} с.",
            "",
            "| Показатель | Значение |", "|---|---|",
            f"| Статус задания | {job['status']} |",
            f"| Время | {r['seconds']:.1f} с |",
            f"| Обработано | {job['processed_ids']} из {job['total_ids']} |",
            f"| Элементов в базе | {total_items} |",
            f"| Дубликаты и потери | {'нет' if total_items == s.total_ids else 'ЕСТЬ'} |",
            f"| Скорость | {r['ids_per_second']:.0f} ид./с |",
            f"| Запросов агентов | {r['requests']} ({r['requests_per_second']:.0f} в секунду) |",
            f"| Интерфейс, 95-й перцентиль (задание и панель) | {ms(r['ui_p95'])} мс |",
            f"| Ошибки | {r['failures'] or 'нет'} |",
            "",
            "| Метод | Вызовов | p50, мс | p95, мс | p99, мс | Максимум, мс |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for op, row in r["latency"].items():
            lines.append(
                f"| {op} | {row['count']} | {ms(row['p50'])} | {ms(row['p95'])} | "
                f"{ms(row['p99'])} | {ms(row['max'])} |"
            )
        lines.append("")
    return "\n".join(lines)


def read_password() -> str:
    return Path(PASSWORD_FILE).read_text().strip()


async def run() -> tuple[str, str]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", help="запустить только этот сценарий")
    parser.add_argument("--out", default="/out/load-results.md")
    args = parser.parse_args()
    verify = ssl.create_default_context(cafile=CA_FILE) if BASE.startswith("https") else False
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    timeout = httpx.Timeout(60.0)
    async with (
        httpx.AsyncClient(base_url=BASE, verify=verify, timeout=timeout) as admin,
        httpx.AsyncClient(base_url=BASE, verify=verify, timeout=timeout, limits=limits) as http,
    ):
        me = await admin.post("/api/auth/login", json={
            "login": "admin", "password": read_password(),
        })
        me.raise_for_status()
        admin.headers["X-CSRF-Token"] = me.json()["csrf_token"]
        issued = (await admin.post("/api/agent-tokens", json={})).json()
        results = []
        try:
            for scenario in SCENARIOS:
                if args.only and scenario.name not in args.only:
                    continue
                print(f"== {scenario.name}", flush=True)
                result = await run_scenario(scenario, admin, http, issued["token"])
                print(
                    f"   {result['job']['status']} за {result['seconds']:.0f} с, "
                    f"{result['ids_per_second']:.0f} ид./с, ошибки: {result['failures'] or 'нет'}",
                    flush=True,
                )
                results.append(result)
        finally:
            await admin.post(f"/api/agent-tokens/{issued['id']}/revoke")
    return report(results), args.out


def main() -> None:
    text, out = asyncio.run(run())
    Path(out).write_text(text, encoding="utf-8")
    print(json.dumps({"written": out}))


if __name__ == "__main__":
    main()
