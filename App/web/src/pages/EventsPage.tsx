import { type FormEvent, useCallback, useEffect, useState } from "react";

import { type EventFilter, type EventRow, listEvents } from "../api";
import { ErrorMessage } from "../components";
import { dateTime } from "../format";
import { errorText } from "../usePolling";

const PAGE_SIZE = 100;
const REFRESH_MS = 5000;

function toIso(local: string): string | undefined {
  return local ? new Date(local).toISOString() : undefined;
}

export function EventsPage({ preset }: { preset: EventFilter }) {
  const [form, setForm] = useState<Record<string, string>>({ ...preset });
  const [filter, setFilter] = useState<EventFilter>(preset);
  const [rows, setRows] = useState<EventRow[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const page = await listEvents(filter);
      setRows((current) => {
        // Подгруженные старые страницы при обновлении верхней не должны пропадать.
        const fresh = new Set(page.map((row) => row.id));
        const oldest = page.length ? page[page.length - 1].id : Infinity;
        return [...page, ...current.filter((row) => !fresh.has(row.id) && row.id < oldest)];
      });
      setHasMore((current) => current || page.length === PAGE_SIZE);
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [filter]);

  useEffect(() => {
    setRows([]);
    setHasMore(false);
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const more = async () => {
    try {
      const page = await listEvents(filter, rows[rows.length - 1]?.id);
      setRows((current) => [...current, ...page]);
      setHasMore(page.length === PAGE_SIZE);
    } catch (err) {
      setError(errorText(err));
    }
  };

  const apply = (event: FormEvent) => {
    event.preventDefault();
    const { since, until, ...rest } = form;
    const clean = Object.fromEntries(Object.entries(rest).filter(([, value]) => value));
    setFilter({ ...clean, since: toIso(since ?? ""), until: toIso(until ?? "") });
  };

  const field = (key: string, label: string, type = "text") => (
    <label>
      {label}
      <input
        type={type}
        value={form[key] ?? ""}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
      />
    </label>
  );

  return (
    <>
      <h1>Журнал событий</h1>
      <form className="card filters" onSubmit={apply}>
        {field("job_id", "Задание", "number")}
        {field("worker_id", "Узел", "number")}
        {field("external_id", "ID элемента", "number")}
        <label>
          Уровень
          <select
            value={form.level ?? ""}
            onChange={(e) => setForm({ ...form, level: e.target.value })}
          >
            <option value="">Все</option>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="error">error</option>
          </select>
        </label>
        {field("result", "Статус")}
        {field("q", "Текст")}
        {field("since", "С", "datetime-local")}
        {field("until", "По", "datetime-local")}
        <button type="submit">Найти</button>
      </form>
      <ErrorMessage text={error} />
      {rows.length === 0 && !error && <p className="muted">Событий нет.</p>}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Время</th>
                <th>Уровень</th>
                <th>Компонент</th>
                <th>Операция</th>
                <th>Статус</th>
                <th>Задание</th>
                <th>Узел</th>
                <th>Блок</th>
                <th>ID</th>
                <th>Код</th>
                <th>Сообщение</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="nowrap">{dateTime(row.at)}</td>
                  <td>{row.level}</td>
                  <td>{row.component}</td>
                  <td className="mono nowrap">{row.operation}</td>
                  <td>{row.result}</td>
                  <td>{row.job_id ? <a href={`#/jobs/${row.job_id}`}>{row.job_id}</a> : "—"}</td>
                  <td>
                    {row.worker_id ? <a href={`#/workers/${row.worker_id}`}>{row.worker_id}</a> : "—"}
                  </td>
                  <td>{row.chunk_id ?? "—"}</td>
                  <td>{row.external_id ?? "—"}</td>
                  <td className="mono">{row.error_code ?? "—"}</td>
                  <td>{row.message ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {hasMore && (
        <p>
          <button type="button" className="button-quiet" onClick={() => void more()}>
            Показать ещё
          </button>
        </p>
      )}
    </>
  );
}
