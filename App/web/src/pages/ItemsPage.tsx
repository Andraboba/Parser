import { type FormEvent, useCallback, useEffect, useState } from "react";

import { type Item, type ItemFilter, itemsCsvUrl, listItems } from "../api";
import { ErrorMessage } from "../components";
import { count, dateTime, ITEM_STATUSES, statusLabel } from "../format";
import { errorText } from "../usePolling";

export function ItemsPage({ jobId }: { jobId: number }) {
  const [status, setStatus] = useState("");
  const [worker, setWorker] = useState("");
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<ItemFilter>({});
  const [items, setItems] = useState<Item[]>([]);
  const [nextAfter, setNextAfter] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (afterId?: number) => {
      setBusy(true);
      setError(null);
      try {
        const page = await listItems(jobId, filter, afterId);
        setItems((current) => (afterId === undefined ? page.items : [...current, ...page.items]));
        setNextAfter(page.next_after_id);
      } catch (err) {
        setError(errorText(err));
      } finally {
        setBusy(false);
      }
    },
    [jobId, filter],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const apply = (event: FormEvent) => {
    event.preventDefault();
    setFilter({ status: status || undefined, worker: worker || undefined, q: q || undefined });
  };

  return (
    <>
      <p>
        <a href={`#/jobs/${jobId}`}>← Задание № {jobId}</a>
      </p>
      <h1>Элементы задания № {jobId}</h1>
      <form className="card filters" onSubmit={apply}>
        <label>
          Статус
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Все</option>
            {ITEM_STATUSES.map((value) => (
              <option key={value} value={value}>
                {statusLabel(value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Узел
          <input value={worker} onChange={(e) => setWorker(e.target.value)} placeholder="worker-01" />
        </label>
        <label>
          Поиск
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="ID или название" />
        </label>
        <button type="submit" disabled={busy}>
          Найти
        </button>
        <a className="button-link" href={itemsCsvUrl(jobId, filter)}>
          Экспорт CSV
        </a>
      </form>
      <ErrorMessage text={error} />
      {items.length === 0 && !busy && <p className="muted">По этому условию элементов нет.</p>}
      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Статус</th>
                <th>Название</th>
                <th>Узел</th>
                <th className="num">HTTP</th>
                <th>Ошибка</th>
                <th className="num">Размер, байт</th>
                <th className="num">Попыток</th>
                <th>Обновлён</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.external_id}>
                  <td className="mono">{item.external_id}</td>
                  <td className="nowrap">{statusLabel(item.status)}</td>
                  <td className="cell-title" title={item.file_path ?? undefined}>
                    {item.title ?? "—"}
                  </td>
                  <td className="nowrap">{item.worker ?? "—"}</td>
                  <td className="num">{item.http_status ?? "—"}</td>
                  <td className="mono">{item.error_class ?? "—"}</td>
                  <td className="num">{item.file_size === null ? "—" : count(item.file_size)}</td>
                  <td className="num">{item.attempts}</td>
                  <td className="nowrap">{dateTime(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {nextAfter !== null && (
        <p>
          <button
            type="button"
            className="button-quiet"
            disabled={busy}
            onClick={() => void load(nextAfter)}
          >
            Показать ещё
          </button>
        </p>
      )}
    </>
  );
}
