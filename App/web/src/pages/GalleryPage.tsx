import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { listVideos, type Video, type VideoFilter, videoUrl } from "../api";
import { ErrorMessage } from "../components";
import { bytes, count, dateTime } from "../format";
import { errorText } from "../usePolling";

function Player({ video, onClose }: { video: Video; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [failed, setFailed] = useState(!video.playable);

  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    return () => node?.close();
  }, []);

  return (
    <dialog ref={dialog} className="player" onClose={onClose} aria-label={video.title ?? video.file_name}>
      <div className="player-head">
        <h2>{video.title ?? `Материал ${video.external_id}`}</h2>
        <button type="button" className="button-quiet" onClick={() => dialog.current?.close()}>
          Закрыть
        </button>
      </div>
      {failed ? (
        <div className="player-fallback">
          <p>Браузер не может воспроизвести этот файл ({video.mime ?? "неизвестный формат"}).</p>
          <p className="muted">Скачайте его и откройте в видеоплеере.</p>
        </div>
      ) : (
        // Файл идёт с рабочего сервера кусками по Range: перемотка не скачивает его целиком.
        <video
          className="player-video"
          src={videoUrl(video.id)}
          controls
          autoPlay
          preload="metadata"
          onError={() => setFailed(true)}
        />
      )}
      <dl className="stats">
        <div className="stat">
          <dt>ID</dt>
          <dd className="mono">{video.external_id}</dd>
        </div>
        <div className="stat">
          <dt>Задание</dt>
          <dd>
            <a href={`#/jobs/${video.job_id}`}>№ {video.job_id}</a>
          </dd>
        </div>
        <div className="stat">
          <dt>Узел</dt>
          <dd>{video.worker ?? "—"}</dd>
        </div>
        <div className="stat">
          <dt>Размер</dt>
          <dd>{bytes(video.size)}</dd>
        </div>
        <div className="stat">
          <dt>Загружено</dt>
          <dd>{dateTime(video.created_at)}</dd>
        </div>
        <div className="stat">
          <dt>Файл</dt>
          <dd className="mono cell-title" title={video.file_name}>
            {video.file_name}
          </dd>
        </div>
      </dl>
      <div className="actions-group">
        <a className="button-link" href={videoUrl(video.id, true)}>
          Скачать
        </a>
        {/^https?:\/\//i.test(video.page_url ?? "") && (
          <a className="button-link" href={video.page_url ?? undefined} target="_blank" rel="noreferrer noopener">
            Страница источника
          </a>
        )}
      </div>
    </dialog>
  );
}

function Card({ video, onOpen }: { video: Video; onOpen: () => void }) {
  return (
    <li className="video-card">
      <button type="button" className="video-thumb" onClick={onOpen} aria-label={`Смотреть: ${video.title ?? video.file_name}`}>
        <span className="video-play" aria-hidden="true">
          ▶
        </span>
        <span className="video-format">{(video.mime ?? "файл").replace(/^video\//, "").toUpperCase()}</span>
      </button>
      <div className="video-meta">
        <button type="button" className="video-title" onClick={onOpen} title={video.title ?? video.file_name}>
          {video.title ?? `Материал ${video.external_id}`}
        </button>
        <span className="muted-inline">
          ID {video.external_id} · {bytes(video.size)} · {video.worker ?? "—"}
        </span>
      </div>
    </li>
  );
}

export function GalleryPage() {
  const [q, setQ] = useState("");
  const [jobId, setJobId] = useState("");
  const [worker, setWorker] = useState("");
  const [filter, setFilter] = useState<VideoFilter>({});
  const [videos, setVideos] = useState<Video[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [open, setOpen] = useState<Video | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (beforeId?: number) => {
      setBusy(true);
      setError(null);
      try {
        const page = await listVideos(filter, beforeId);
        setVideos((current) => (beforeId === undefined ? page.items : [...current, ...page.items]));
        setTotal(page.total);
        setNextBefore(page.next_before_id);
      } catch (err) {
        setError(errorText(err));
      } finally {
        setBusy(false);
      }
    },
    [filter],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // Поиск по мере ввода, с паузой: не дёргать базу на каждую букву.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setFilter((current) =>
        (current.q ?? "") === q.trim() ? current : { ...current, q: q.trim() || undefined },
      );
    }, 350);
    return () => window.clearTimeout(timer);
  }, [q]);

  const apply = (event: FormEvent) => {
    event.preventDefault();
    setFilter({ q: q.trim() || undefined, job_id: jobId || undefined, worker: worker || undefined });
  };

  return (
    <>
      <h1>Галерея</h1>
      <form className="card filters" onSubmit={apply} role="search">
        <label className="field-wide">
          Поиск
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Название, ID или адрес страницы"
            autoFocus
          />
        </label>
        <label>
          Задание
          <input
            type="number"
            min={1}
            value={jobId}
            onChange={(e) => setJobId(e.target.value)}
            placeholder="Все"
          />
        </label>
        <label>
          Узел
          <input value={worker} onChange={(e) => setWorker(e.target.value)} placeholder="Все" />
        </label>
        <button type="submit" disabled={busy}>
          Найти
        </button>
      </form>
      <ErrorMessage text={error} />
      {total !== null && (
        <p className="muted" aria-live="polite">
          {total === 0
            ? filter.q || filter.job_id || filter.worker
              ? "Ничего не найдено."
              : "Загруженных видео пока нет. Они появятся, когда агенты скачают файлы."
            : `Найдено: ${count(total)}`}
        </p>
      )}
      {videos.length > 0 && (
        <ul className="video-grid">
          {videos.map((video) => (
            <Card key={video.id} video={video} onOpen={() => setOpen(video)} />
          ))}
        </ul>
      )}
      {nextBefore !== null && (
        <p>
          <button type="button" className="button-quiet" disabled={busy} onClick={() => void load(nextBefore)}>
            Показать ещё
          </button>
        </p>
      )}
      {open && <Player key={open.id} video={open} onClose={() => setOpen(null)} />}
    </>
  );
}
