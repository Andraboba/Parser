"""Идемпотентное сохранение результатов: повтор обновляет запись и не создаёт вторую."""
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import batched

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.domain.paths import is_inside
from coordinator.domain.statuses import ItemStatus
from coordinator.storage.models import Download, File, IdempotencyKey, Item, MediaMetadata, Worker

# Предел PostgreSQL — 65 535 параметров на запрос, а блок бывает на 10 000 элементов.
_STATEMENT_ROWS = 1000


@dataclass(frozen=True)
class FileRow:
    path: str
    size: int
    checksum: str | None = None
    mime: str | None = None
    resumed: bool = False


@dataclass(frozen=True)
class ResultRow:
    external_id: int
    status: str
    error_class: str | None = None
    http_status: int | None = None
    title: str | None = None
    page_url: str | None = None
    download_url: str | None = None
    size: int | None = None
    mime: str | None = None
    file: FileRow | None = None

    @property
    def has_metadata(self) -> bool:
        return any(v is not None for v in (self.title, self.page_url, self.download_url))


def _validated(worker: Worker, results: Sequence[ResultRow]) -> Iterator[ResultRow]:
    """Последний результат по каждому идентификатору; файл вне каталога узла отклоняется."""
    for row in {r.external_id: r for r in results}.values():
        if row.file is not None and not is_inside(worker.storage_path, row.file.path):
            row = ResultRow(
                external_id=row.external_id, status=ItemStatus.FAILED, error_class="invalid_path",
                http_status=row.http_status, title=row.title, page_url=row.page_url,
                download_url=row.download_url, size=row.size, mime=row.mime,
            )
        yield row


async def save_results(
    session: AsyncSession, job_id: int, worker: Worker, results: Sequence[ResultRow],
) -> list[int]:
    """Сохраняет результаты в текущей транзакции. Возвращает id элементов с отклонённым файлом."""
    rows = list(_validated(worker, results))
    rejected = [r.external_id for r in rows if r.error_class == "invalid_path"]
    for part in batched(rows, _STATEMENT_ROWS, strict=False):
        await _save_part(session, job_id, worker.id, part)
    return rejected


async def _save_part(
    session: AsyncSession, job_id: int, worker_id: int, rows: tuple[ResultRow, ...],
) -> None:
    stmt = insert(Item).values([
        {"job_id": job_id, "external_id": r.external_id, "status": r.status,
         "error_class": r.error_class, "http_status": r.http_status, "worker_id": worker_id}
        for r in rows
    ])
    ids = dict((await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Item.job_id, Item.external_id],
            set_={"status": stmt.excluded.status, "error_class": stmt.excluded.error_class,
                  "http_status": stmt.excluded.http_status, "worker_id": stmt.excluded.worker_id,
                  "attempts": Item.attempts + 1, "updated_at": func.now()},
            # Загруженный файл не должен потеряться из-за поздней повторной проверки.
            where=or_(Item.status != ItemStatus.COMPLETED,
                      stmt.excluded.status == ItemStatus.COMPLETED),
        ).returning(Item.external_id, Item.id)
    )).all())

    with_metadata = [r for r in rows if r.has_metadata and r.external_id in ids]
    if with_metadata:
        meta = insert(MediaMetadata).values([
            {"item_id": ids[r.external_id], "title": r.title, "page_url": r.page_url,
             "download_url": r.download_url, "size": r.size,
             "mime": r.file.mime if r.file else r.mime,
             "checksum": r.file.checksum if r.file else None}
            for r in with_metadata
        ])
        await session.execute(meta.on_conflict_do_update(
            index_elements=[MediaMetadata.item_id],
            set_={c: getattr(meta.excluded, c)
                  for c in ("title", "page_url", "download_url", "size", "mime", "checksum")},
        ))

    with_file = [r for r in rows if r.file is not None and r.external_id in ids]
    if with_file:
        await _save_files(session, ids, worker_id, with_file)


async def _save_files(
    session: AsyncSession, ids: dict[int, int], worker_id: int, rows: list[ResultRow],
) -> None:
    stmt = insert(Download).values([
        {"item_id": ids[r.external_id], "worker_id": worker_id, "state": ItemStatus.COMPLETED,
         "bytes_done": r.file.size, "resumed": r.file.resumed, "finished_at": func.now()}
        for r in rows if r.file
    ])
    download_ids = dict((await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Download.item_id],
            set_={"worker_id": stmt.excluded.worker_id, "state": stmt.excluded.state,
                  "bytes_done": stmt.excluded.bytes_done, "resumed": stmt.excluded.resumed,
                  "finished_at": stmt.excluded.finished_at},
        ).returning(Download.item_id, Download.id)
    )).all())
    files = insert(File).values([
        {"download_id": download_ids[ids[r.external_id]], "storage_path": r.file.path,
         "size": r.file.size, "checksum": r.file.checksum, "mime": r.file.mime}
        for r in rows if r.file
    ])
    await session.execute(files.on_conflict_do_update(
        index_elements=[File.download_id, File.storage_path],
        set_={"size": files.excluded.size, "checksum": files.excluded.checksum,
              "mime": files.excluded.mime},
    ))


async def claim_idempotency_key(session: AsyncSession, worker_id: int, key: str) -> bool:
    """Запоминает ключ в текущей транзакции. False — запрос с этим ключом уже был применён."""
    claimed = await session.scalar(
        insert(IdempotencyKey).values(worker_id=worker_id, key=key)
        .on_conflict_do_nothing().returning(IdempotencyKey.key)
    )
    return claimed is not None


@dataclass(frozen=True)
class ItemView:
    external_id: int
    status: str
    error_class: str | None
    http_status: int | None
    attempts: int
    worker: str | None
    updated_at: object
    title: str | None
    page_url: str | None
    download_url: str | None
    mime: str | None
    checksum: str | None
    file_path: str | None
    file_size: int | None


def _items_query(job_id: int, status: str | None, worker: str | None, q: str | None):
    stmt = (
        select(
            Item.external_id, Item.status, Item.error_class, Item.http_status, Item.attempts,
            Worker.name, Item.updated_at, MediaMetadata.title, MediaMetadata.page_url,
            MediaMetadata.download_url, MediaMetadata.mime, MediaMetadata.checksum,
            File.storage_path, File.size,
        )
        .outerjoin(Worker, Worker.id == Item.worker_id)
        .outerjoin(MediaMetadata, MediaMetadata.item_id == Item.id)
        .outerjoin(Download, Download.item_id == Item.id)
        .outerjoin(File, File.download_id == Download.id)
        .where(Item.job_id == job_id)
        .ext(distinct_on(Item.external_id))
    )
    if status:
        stmt = stmt.where(Item.status == status)
    if worker:
        stmt = stmt.where(Worker.name == worker)
    if q:
        condition = MediaMetadata.title.icontains(q, autoescape=True)
        if q.isdecimal():
            condition = or_(condition, Item.external_id == int(q))
        stmt = stmt.where(condition)
    return stmt


async def list_items(
    session: AsyncSession, job_id: int, *, status: str | None = None, worker: str | None = None,
    q: str | None = None, after_id: int | None = None, limit: int = 100,
) -> list[ItemView]:
    """Страница элементов. Постранично по идентификатору: OFFSET на миллионах строк медленный."""
    stmt = _items_query(job_id, status, worker, q)
    if after_id is not None:
        stmt = stmt.where(Item.external_id > after_id)
    rows = await session.execute(stmt.order_by(Item.external_id, File.id.desc()).limit(limit))
    return [ItemView(*row) for row in rows]


async def stream_items(
    session: AsyncSession, job_id: int, *, status: str | None = None, worker: str | None = None,
    q: str | None = None,
):
    stmt = _items_query(job_id, status, worker, q).order_by(Item.external_id, File.id.desc())
    async for row in await session.stream(stmt.execution_options(yield_per=1000)):
        yield ItemView(*row)


async def error_summary(session: AsyncSession, job_id: int) -> list[tuple[str, str | None, int]]:
    rows = await session.execute(
        select(Item.status, Item.error_class, func.count())
        .where(Item.job_id == job_id, Item.error_class.is_not(None))
        .group_by(Item.status, Item.error_class)
        .order_by(func.count().desc())
    )
    return [tuple(row) for row in rows]
