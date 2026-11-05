"""Схемы запросов и ответов HTTP API (раздел 5 плана)."""
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from coordinator.domain.chunks import MAX_CHUNK_SIZE, MIN_CHUNK_SIZE
from coordinator.domain.ranges import MAX_ID, RangeSpec, from_bounds, parse_range
from coordinator.domain.retry import error_group
from coordinator.domain.statuses import ItemStatus, JobStatus
from coordinator.storage.jobs import JobParams, JobView
from coordinator.storage.results import FileRow, ItemView, ResultRow
from coordinator.storage.workers import WorkerView


class ErrorOut(BaseModel):
    """Ошибка API: человекочитаемое описание и код, по которому клиент принимает решение."""

    detail: str | list[Any]
    code: str


AGENT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorOut, "description": "Нет токена, токен неизвестен, отозван или истёк"},
    403: {"model": ErrorOut, "description": "Токен принадлежит другому узлу"},
    404: {"model": ErrorOut, "description": "Узел не найден, нужна повторная регистрация"},
    409: {"model": ErrorOut, "description": "Конфликт аренды или версии протокола"},
    422: {"model": ErrorOut, "description": "Некорректный запрос, повторять вслепую нельзя"},
    503: {"model": ErrorOut, "description": "База недоступна, запрос не применён, нужен повтор"},
}

# Значения вне диапазона колонок должны давать 422, а не ошибку базы.
Int32 = Annotated[int, Field(ge=0, le=2**31 - 1)]
Int64 = Annotated[int, Field(ge=0, le=2**63 - 1)]
ExternalId = Annotated[int, Field(ge=0, le=MAX_ID)]


class JobWorkerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    threads: int | None = Field(default=None, ge=1, le=10_000)


class JobCreate(BaseModel):
    """Диапазон задаётся строкой range (ТЗ, п. 3) либо границами range_from и range_to."""

    range: str | None = Field(default=None, max_length=64)
    range_from: ExternalId | None = None
    range_to: ExternalId | None = None
    chunk_size: int = Field(default=1000, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE)
    # Потоки: глобальное значение и индивидуальное в workers, оно важнее (ТЗ, п. 4).
    threads: int | None = Field(default=None, ge=1, le=10_000)
    workers: list[JobWorkerIn] = Field(default=[], max_length=1000)
    request_timeout_seconds: float = Field(default=20.0, gt=0, le=3600)
    max_retries: int = Field(default=4, ge=0, le=100)
    global_rate_limit_rps: float | None = Field(default=None, gt=0, le=1_000_000)
    max_ids: ExternalId | None = Field(default=None, ge=1)
    max_duration_seconds: Int32 | None = Field(default=None, ge=1)
    # По умолчанию отключено: пропуски не доказывают отсутствие материалов дальше (ТЗ, п. 10).
    stop_on_consecutive_missing: Int32 | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_range(self) -> Self:
        names = [w.name for w in self.workers]
        if len(set(names)) != len(names):
            raise ValueError("узел указан в задании дважды")
        self.range_spec()
        return self

    def range_spec(self) -> RangeSpec:
        if self.range is not None:
            return parse_range(self.range)
        return from_bounds(self.range_from, self.range_to)

    def to_params(self) -> JobParams:
        return JobParams(
            range=self.range_spec(), chunk_size=self.chunk_size, threads=self.threads,
            request_timeout_seconds=self.request_timeout_seconds, max_retries=self.max_retries,
            global_rate_limit_rps=self.global_rate_limit_rps, max_ids=self.max_ids,
            max_duration_seconds=self.max_duration_seconds,
            stop_on_consecutive_missing=self.stop_on_consecutive_missing,
            worker_threads=tuple((w.name, w.threads) for w in self.workers),
        )


class JobOut(BaseModel):
    id: int
    range_from: int
    range_to: int | None
    chunk_size: int
    status: JobStatus
    finish_reason: str | None
    run_attempt: int
    threads: int | None
    request_timeout_seconds: float
    max_retries: int
    global_rate_limit_rps: float | None
    max_ids: int | None
    max_duration_seconds: int | None
    stop_on_consecutive_missing: int | None
    workers: dict[str, int | None]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    total_ids: int | None
    processed_ids: int
    chunks_total: int | None
    chunks_done: int
    chunks_leased: int
    chunks_failed: int
    items: dict[str, int]

    @classmethod
    def from_view(cls, view: JobView) -> Self:
        job = view.job
        return cls(
            id=job.id, range_from=job.range_from, range_to=job.range_to,
            chunk_size=job.chunk_size, status=job.status, finish_reason=job.finish_reason,
            run_attempt=job.run_attempt, threads=job.threads,
            request_timeout_seconds=job.request_timeout_seconds, max_retries=job.max_retries,
            global_rate_limit_rps=job.global_rate_limit_rps, max_ids=job.max_ids,
            max_duration_seconds=job.max_duration_seconds,
            stop_on_consecutive_missing=job.stop_on_consecutive_missing,
            workers=view.worker_threads, created_at=job.created_at, started_at=job.started_at,
            finished_at=job.finished_at, total_ids=view.total_ids,
            processed_ids=view.processed_ids, chunks_total=view.chunks_total,
            chunks_done=view.chunks_done, chunks_leased=view.chunks_leased,
            chunks_failed=view.chunks_failed, items=view.items,
        )


class Limits(BaseModel):
    cpus: float | None = Field(default=None, ge=0)
    memory_mb: Int32 | None = None


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provisioning_mode: str = Field(default="docker", max_length=16)
    container_id: str | None = Field(default=None, max_length=128)
    image: str | None = Field(default=None, max_length=255)
    image_digest: str | None = Field(default=None, max_length=255)
    agent_version: str = Field(max_length=32)
    protocol_version: Int32
    threads: int = Field(ge=1, le=10_000)
    limits: Limits = Limits()
    storage_path: str | None = Field(default=None, max_length=1024)


class RegisterResponse(BaseModel):
    worker_id: int
    heartbeat_interval: int
    lease_ttl: int


class WorkerMetricsIn(BaseModel):
    """Снимок узла. Неизвестные поля идут в extra: метрики добавляются без смены протокола."""

    model_config = ConfigDict(extra="allow")

    cpu_cores: float | None = None
    cpu_limit: float | None = None
    memory_bytes: Int64 | None = None
    memory_limit: Int64 | None = None
    disk_total_bytes: Int64 | None = None
    disk_free_bytes: Int64 | None = None
    disk_used_bytes: Int64 | None = None
    load1: float | None = None
    net_rx_bps: Int64 | None = None
    net_tx_bps: Int64 | None = None
    uptime_seconds: float | None = None


class HeartbeatRequest(BaseModel):
    status: str | None = Field(default=None, max_length=16)
    free_slots: Int32 | None = None
    # Блоки в работе у агента. Не передано — сверки нет, продлеваются все блоки узла.
    active_chunks: list[Int64] | None = None
    metrics: WorkerMetricsIn = WorkerMetricsIn()
    counters: dict[str, int | float] = {}


class HeartbeatResponse(BaseModel):
    """Подтверждение и указания агенту. Поле может быть пустым: тогда ничего менять не нужно."""

    ok: bool = True
    lease_ttl: int
    cancel_chunks: list[int] = []
    threads: int | None = None
    rps_share: float | None = None


class LeaseRequest(BaseModel):
    free_slots: Int32 = 1


class LeaseResponse(BaseModel):
    chunk_id: int
    job_id: int
    range_start: int
    range_end: int
    next_id: int
    attempt: int
    lease_expires_at: datetime
    lease_ttl: int
    rps_share: float
    threads: int | None = None
    request_timeout_seconds: float | None = None
    max_retries: int | None = None


def _clean(limit: int) -> AfterValidator:
    """Текст с внешнего сайта: PostgreSQL не принимает NUL, а длинное значение обрезается,
    чтобы один странный материал не отбросил весь пакет результатов."""
    return AfterValidator(lambda value: value.replace("\x00", "")[:limit])


class FileIn(BaseModel):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    size: Int64
    sha256: Annotated[str, _clean(128)] | None = None
    mime: Annotated[str, _clean(128)] | None = None
    resumed: bool = False


class ResultIn(BaseModel):
    external_id: ExternalId
    status: ItemStatus
    error_class: Annotated[str, _clean(64)] | None = None
    http_status: Annotated[int, Field(ge=0, le=999)] | None = None
    title: Annotated[str, _clean(2000)] | None = None
    page_url: Annotated[str, _clean(4000)] | None = None
    download_url: Annotated[str, _clean(4000)] | None = None
    size: Int64 | None = None
    mime: Annotated[str, _clean(128)] | None = None
    file: FileIn | None = None

    def to_row(self) -> ResultRow:
        file = self.file
        return ResultRow(
            external_id=self.external_id, status=self.status, error_class=self.error_class,
            http_status=self.http_status, title=self.title, page_url=self.page_url,
            download_url=self.download_url, size=self.size, mime=self.mime,
            file=file and FileRow(file.path, file.size, file.sha256, file.mime, file.resumed),
        )


class ProgressRequest(BaseModel):
    chunk_id: Int64
    next_id: Int64
    results: list[ResultIn] = Field(default=[], max_length=MAX_CHUNK_SIZE * 2)
    counters: dict[str, int | float] = {}


class ProgressResponse(BaseModel):
    ok: bool = True
    # Задание остановлено: агенту пора прекратить блок и отдать его через complete.
    cancel: bool = False
    rejected: list[int] = []


class CompleteRequest(BaseModel):
    chunk_id: Int64
    next_id: Int64
    results: list[ResultIn] = Field(default=[], max_length=MAX_CHUNK_SIZE * 2)
    summary: dict[str, Any] | None = None


class CompleteResponse(BaseModel):
    ok: bool = True
    rejected: list[int] = []


class FailRequest(BaseModel):
    chunk_id: Int64
    error_class: Annotated[str, Field(min_length=1), _clean(64)]
    message: Annotated[str, _clean(500)] = ""
    next_id: Int64 | None = None


class Ack(BaseModel):
    ok: bool = True


class WorkerOut(BaseModel):
    id: int
    name: str
    provisioning_mode: str
    container_id: str | None
    image: str | None
    image_digest: str | None
    cpu_limit: float | None
    mem_limit_mb: int | None
    threads: int
    desired_threads: int | None
    enabled: bool
    min_free_space_gb: float | None
    rps_limit: float | None
    storage_path: str | None
    status: str
    agent_state: str | None
    free_slots: int | None
    lease_block_reason: str | None
    agent_version: str
    protocol_version: int
    last_ip: str | None
    ssh_host: str | None
    ssh_port: int | None
    ssh_user: str | None
    registered_at: datetime
    last_heartbeat_at: datetime | None
    leased_chunks: int
    current_chunks: list[int]
    metrics: dict[str, Any] | None
    counters: dict[str, Any] | None
    container_state: dict[str, Any] | None

    @classmethod
    def from_view(cls, view: WorkerView) -> Self:
        w = view.worker
        return cls(
            id=w.id, name=w.name, provisioning_mode=w.provisioning_mode,
            container_id=w.container_id, image=w.image, image_digest=w.image_digest,
            cpu_limit=w.cpu_limit, mem_limit_mb=w.mem_limit_mb, threads=w.threads,
            desired_threads=w.desired_threads, enabled=w.enabled,
            min_free_space_gb=w.min_free_space_gb, rps_limit=w.rps_limit,
            storage_path=w.storage_path, status=w.status, agent_state=w.agent_state,
            free_slots=w.free_slots, lease_block_reason=w.lease_block_reason,
            agent_version=w.agent_version,
            protocol_version=w.protocol_version, last_ip=w.last_ip, ssh_host=w.ssh_host,
            ssh_port=w.ssh_port, ssh_user=w.ssh_user, registered_at=w.registered_at,
            last_heartbeat_at=w.last_heartbeat_at, leased_chunks=view.leased_chunks,
            current_chunks=view.current_chunks, metrics=w.metrics, counters=w.counters,
            container_state=w.container_state,
        )


class WorkerPatch(BaseModel):
    """Настройки узла от оператора (ТЗ, п. 11.3, п. 16). Не переданное поле не меняется."""

    desired_threads: int | None = Field(default=None, ge=1, le=10_000)
    enabled: bool | None = None
    min_free_space_gb: float | None = Field(default=None, ge=0)
    rps_limit: float | None = Field(default=None, gt=0)
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)
    # Имя секрета в менеджере секретов, а не сам ключ (ТЗ, п. 11.4).
    ssh_key_ref: str | None = Field(default=None, max_length=255)


class JobPatch(BaseModel):
    """Параметры, которые можно менять на ходу (ТЗ, п. 11.3)."""

    threads: int | None = Field(default=None, ge=1, le=10_000)
    global_rate_limit_rps: float | None = Field(default=None, gt=0, le=1_000_000)
    request_timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    max_retries: int | None = Field(default=None, ge=0, le=100)


class ContainerOut(BaseModel):
    worker: str | None
    id: str
    image: str
    state: str
    status: str | None = None
    restart_count: int = 0
    oom_killed: bool = False
    started_at: str | None = None


class ContainerCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    image: str = Field(min_length=1, max_length=255)
    threads: int = Field(ge=1, le=1000)
    cpus: float = Field(ge=0.1, le=64)
    memory_mb: int = Field(ge=64, le=1024 * 1024)


class ContainerImage(BaseModel):
    image: str = Field(min_length=1, max_length=255)


class ContainerLogs(BaseModel):
    logs: str


class MetricSample(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    collected_at: datetime
    cpu_cores: float | None
    cpu_limit: float | None
    memory_bytes: int | None
    memory_limit: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    load1: float | None
    net_rx_bps: int | None
    net_tx_bps: int | None
    uptime_seconds: float | None
    extra: dict[str, Any] | None
    counters: dict[str, Any] | None


class RateLimitOut(BaseModel):
    base_rps: float
    effective_rps: float | None
    shares: dict[int, float]
    redis_available: bool


class ItemOut(BaseModel):
    external_id: int
    status: str
    error_class: str | None
    error_group: str | None
    http_status: int | None
    attempts: int
    worker: str | None
    updated_at: datetime
    title: str | None
    page_url: str | None
    download_url: str | None
    mime: str | None
    checksum: str | None
    file_path: str | None
    file_size: int | None

    @classmethod
    def from_view(cls, view: ItemView) -> Self:
        return cls(**view.__dict__, error_group=error_group(view.error_class))


class ItemsPage(BaseModel):
    items: list[ItemOut]
    # Передайте значением after_id, чтобы получить следующую страницу. None — страниц больше нет.
    next_after_id: int | None


class ErrorSummaryRow(BaseModel):
    status: str
    error_class: str
    error_group: str | None
    count: int


class RetryOut(BaseModel):
    items_requeued: int
    chunks_created: int
    chunks_requeued: int
    job: JobOut
