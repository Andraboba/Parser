"""Главная панель, предупреждения и экспорт метрик для Prometheus."""
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from coordinator.api.deps import RedisDep, SessionDep, SettingsDep, require_viewer
from coordinator.api.schemas import JobOut
from coordinator.domain.alerts import AlertLimits
from coordinator.storage import monitoring, ratelimit

router = APIRouter(tags=["monitoring"])


class SampleOut(BaseModel):
    at: datetime
    processed_ids: int
    bytes_done: int


class DashboardOut(BaseModel):
    job: JobOut | None
    bytes_done: int
    ids_per_second: float
    average_ids_per_second: float
    bytes_per_second: float
    average_bytes_per_second: float
    eta_seconds: float | None
    active_workers: int
    active_threads: int
    workers_total: int
    samples: list[SampleOut]


class AlertOut(BaseModel):
    code: str
    severity: str
    message: str
    worker_id: int | None
    job_id: int | None


@router.get("/api/dashboard", dependencies=[Depends(require_viewer)])
async def dashboard(
    session: SessionDep, job_id: Annotated[int | None, Query(ge=1)] = None,
) -> DashboardOut:
    data = await monitoring.dashboard(session, job_id)
    return DashboardOut(
        job=JobOut.from_view(data.view) if data.view else None, bytes_done=data.bytes_done,
        ids_per_second=data.rates.ids_per_second,
        average_ids_per_second=data.average_ids_per_second,
        bytes_per_second=data.rates.bytes_per_second,
        average_bytes_per_second=data.average_bytes_per_second, eta_seconds=data.eta_seconds,
        active_workers=data.active_workers, active_threads=data.active_threads,
        workers_total=data.workers_total,
        samples=[SampleOut(**s.__dict__) for s in data.samples],
    )


@router.get("/api/alerts", dependencies=[Depends(require_viewer)])
async def alerts(session: SessionDep, settings: SettingsDep) -> list[AlertOut]:
    return [AlertOut(**a.__dict__) for a in await monitoring.active_alerts(session, settings)]


def _labels(**labels: Any) -> str:
    inner = ",".join(
        '{}="{}"'.format(k, str(v).replace("\\", r"\\").replace('"', r"\"").replace("\n", " "))
        for k, v in labels.items()
    )
    return "{" + inner + "}" if inner else ""


def render_metric(name: str, kind: str, help_text: str, rows: list[tuple[str, float]]) -> str:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    lines += [f"{name}{labels} {value}" for labels, value in rows]
    return "\n".join(lines)


@router.get("/metrics", include_in_schema=False)
async def metrics(session: SessionDep, redis: RedisDep, settings: SettingsDep) -> Response:
    """Формат Prometheus. Маршрут внутренний: nginx его не проксирует, порт не публикуется."""
    limits = AlertLimits()
    workers = await monitoring.worker_snapshots(session, settings)
    totals = await monitoring.totals(session)
    active = await monitoring.active_alerts(session, settings)
    rate = await ratelimit.snapshot(session, redis, settings)
    online = sum(1 for w in workers if w.online and w.enabled)
    jobs = await monitoring.job_snapshots(session, online)

    def per_worker(pick: Any) -> list[tuple[str, float]]:
        return [
            (_labels(worker=w.name), float(pick(w))) for w in workers if pick(w) is not None
        ]

    blocks = [
        render_metric("coord_workers_online", "gauge", "Узлов в сети", [("", online)]),
        render_metric("coord_worker_up", "gauge", "Узел в сети",
                      per_worker(lambda w: int(w.online))),
        render_metric("coord_worker_heartbeat_age_seconds", "gauge", "Возраст heartbeat",
                      per_worker(lambda w: w.heartbeat_age_seconds)),
        render_metric("coord_worker_cpu_cores", "gauge", "Занято ядер CPU",
                      per_worker(lambda w: w.cpu_cores_avg)),
        render_metric("coord_worker_cpu_limit", "gauge", "Лимит CPU контейнера",
                      per_worker(lambda w: w.cpu_limit)),
        render_metric("coord_worker_memory_bytes", "gauge", "Занято памяти",
                      per_worker(lambda w: w.memory_bytes)),
        render_metric("coord_worker_memory_limit_bytes", "gauge", "Лимит памяти контейнера",
                      per_worker(lambda w: w.memory_limit)),
        render_metric("coord_worker_disk_free_bytes", "gauge", "Свободно на диске",
                      per_worker(lambda w: w.disk_free_bytes)),
        render_metric("coord_worker_min_free_bytes", "gauge", "Порог свободного места",
                      per_worker(lambda w: w.min_free_bytes)),
        render_metric("coord_worker_swap_bytes", "gauge", "Использовано swap",
                      per_worker(lambda w: w.swap_used_bytes)),
        render_metric("coord_worker_disk_read_bps", "gauge", "Диск, чтение в секунду",
                      per_worker(lambda w: w.disk_read_bps)),
        render_metric("coord_worker_disk_write_bps", "gauge", "Диск, запись в секунду",
                      per_worker(lambda w: w.disk_write_bps)),
        render_metric("coord_worker_open_files", "gauge", "Открытых файлов",
                      per_worker(lambda w: w.open_files)),
        render_metric("coord_worker_connections", "gauge", "TCP-соединений",
                      per_worker(lambda w: w.connections)),
        render_metric("coord_worker_recent_checked", "gauge", "Проверок за минуту",
                      per_worker(lambda w: w.checked_recently)),
        render_metric("coord_worker_recent_failed", "gauge", "Ошибок за минуту",
                      per_worker(lambda w: w.failed_recently)),
        render_metric("coord_worker_recent_rate_limited", "gauge", "Ответов 429 за минуту",
                      per_worker(lambda w: w.rate_limited_recently)),
        render_metric("coord_worker_container_restarts", "gauge", "Перезапуски контейнера",
                      per_worker(lambda w: w.restart_count)),
        render_metric("coord_worker_oom_killed", "gauge", "Контейнер убит по памяти",
                      per_worker(lambda w: int(w.oom_killed))),
        render_metric("coord_chunks", "gauge", "Блоков по статусам", [
            (_labels(status=k), v) for k, v in totals.chunks.items()
        ]),
        render_metric("coord_items", "gauge", "Элементов по статусам", [
            (_labels(status=k), v) for k, v in totals.items.items()
        ]),
        render_metric("coord_jobs", "gauge", "Заданий по статусам", [
            (_labels(status=k), v) for k, v in totals.jobs.items()
        ]),
        render_metric("coord_oldest_pending_seconds", "gauge", "Возраст старейшего блока в очереди",
                      [("", totals.oldest_pending_seconds)]),
        render_metric("coord_bytes_downloaded", "gauge", "Загружено байт",
                      [("", totals.bytes_done)]),
        render_metric("coord_job_ids_per_second", "gauge", "Скорость задания", [
            (_labels(job=j.id), j.current_ids_per_second) for j in jobs
        ]),
        render_metric("coord_job_average_ids_per_second", "gauge", "Средняя скорость задания", [
            (_labels(job=j.id), j.average_ids_per_second) for j in jobs
        ]),
        render_metric("coord_job_seconds_since_progress", "gauge", "Без продвижения, секунд", [
            (_labels(job=j.id), j.seconds_since_progress) for j in jobs
            if j.seconds_since_progress is not None
        ]),
        render_metric("coord_rate_limit_base_rps", "gauge", "Базовый общий лимит запросов",
                      [("", float(rate["base_rps"]))]),
        render_metric("coord_rate_limit_effective_rps", "gauge", "Текущий общий лимит запросов",
                      [("", float(rate["effective_rps"] or rate["base_rps"]))]),
        render_metric("coord_alerts", "gauge", "Активные предупреждения", [
            (
                _labels(
                    code=a.code, severity=a.severity, worker=a.worker_id or "", job=a.job_id or "",
                ),
                1,
            )
            for a in active
        ]),
        render_metric("coord_alert_stall_threshold_seconds", "gauge", "Порог простоя задания",
                      [("", limits.stall_seconds)]),
    ]
    return Response("\n".join(blocks) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")
