import { useCallback, useState } from "react";

import { getDashboard, listAlerts, listJobs } from "../api";
import { ErrorMessage, JobActions, ProgressBar, StatusBadge } from "../components";
import { bytes, count, duration, rangeText, rate } from "../format";
import { SpeedChart } from "../SpeedChart";
import { usePolling } from "../usePolling";

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile">
      <dt>{label}</dt>
      <dd>{value}</dd>
      {note && <small>{note}</small>}
    </div>
  );
}

export function Alerts() {
  const { data: alerts } = usePolling(listAlerts);
  if (!alerts || alerts.length === 0) return null;
  return (
    <ul className="alerts" aria-label="Предупреждения">
      {alerts.map((alert) => (
        <li
          key={`${alert.code}-${alert.worker_id}-${alert.job_id}`}
          className={`alert ${alert.severity === "critical" ? "alert-critical" : ""}`}
        >
          <span className="alert-tag">{alert.severity === "critical" ? "Срочно" : "Внимание"}</span>
          <span>
            {alert.worker_id ? (
              <a href={`#/workers/${alert.worker_id}`}>{alert.message}</a>
            ) : alert.job_id ? (
              <a href={`#/jobs/${alert.job_id}`}>{alert.message}</a>
            ) : (
              alert.message
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function DashboardPage() {
  const [selected, setSelected] = useState<number | undefined>(undefined);
  const load = useCallback(() => getDashboard(selected), [selected]);
  const { data, error, refresh } = usePolling(load);
  const { data: jobs } = usePolling(listJobs);
  const job = data?.job ?? null;
  const items = job?.items ?? {};
  const failed = (items.failed ?? 0) + (items.forbidden ?? 0) + (items.rate_limited ?? 0);

  return (
    <>
      <div className="job-head">
        <h1>Главная панель</h1>
        {jobs && jobs.length > 0 && (
          <label className="job-select">
            Задание
            <select
              value={selected ?? job?.id ?? ""}
              onChange={(e) => setSelected(Number(e.target.value))}
            >
              {jobs.map((row) => (
                <option key={row.id} value={row.id}>
                  № {row.id}: {rangeText(row.range_from, row.range_to)}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <Alerts />
      <ErrorMessage text={error} />
      {data && !job && (
        <p className="muted">
          Заданий пока нет. <a href="#/jobs">Создать задание</a>
        </p>
      )}
      {data && job && (
        <>
          <section className="card">
            <div className="job-head">
              <span>
                <a href={`#/jobs/${job.id}`}>Задание № {job.id}</a>{" "}
                <StatusBadge status={job.status} />
              </span>
              <JobActions job={job} onDone={refresh} />
            </div>
            <ProgressBar done={job.processed_ids} total={job.total_ids} />
            <p className="muted">Диапазон: {rangeText(job.range_from, job.range_to)}</p>
          </section>
          <dl className="tiles">
            <Tile label="Обработано" value={count(job.processed_ids)} />
            <Tile label="Найдено" value={count((items.found ?? 0) + (items.completed ?? 0))} />
            <Tile label="Загружено файлов" value={count(items.completed ?? 0)} />
            <Tile label="Ошибок" value={count(failed)} />
            <Tile
              label="Скорость обработки"
              value={`${rate(data.ids_per_second)} ид./с`}
              note={`в среднем ${rate(data.average_ids_per_second)} ид./с`}
            />
            <Tile
              label="Скорость загрузки"
              value={`${bytes(data.bytes_per_second)}/с`}
              note={`в среднем ${bytes(data.average_bytes_per_second)}/с`}
            />
            <Tile label="Загружено данных" value={bytes(data.bytes_done)} />
            <Tile
              label="Осталось времени"
              value={data.eta_seconds === null ? "неопределённо" : duration(data.eta_seconds)}
            />
            <Tile
              label="Активных узлов"
              value={count(data.active_workers)}
              note={`всего ${count(data.workers_total)}`}
            />
            <Tile label="Активных потоков" value={count(data.active_threads)} />
          </dl>
          <section className="card">
            <h2>Скорость обработки, идентификаторов в секунду</h2>
            <SpeedChart samples={data.samples} />
          </section>
        </>
      )}
    </>
  );
}
