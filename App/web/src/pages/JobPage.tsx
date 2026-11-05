import { type FormEvent, useCallback, useState } from "react";

import { useAuth } from "../auth";
import { getJob, jobAction, jobErrors, patchJob } from "../api";
import { ErrorMessage, JobActions, ProgressBar, StatusBadge } from "../components";
import { count, dateTime, rangeText, statusLabel } from "../format";
import { errorText, usePolling } from "../usePolling";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

const ERROR_ITEM_STATUSES = ["failed", "rate_limited", "retry_wait", "cancelled"];

export function JobPage({ id }: { id: number }) {
  const { canOperate } = useAuth();
  const [threads, setThreads] = useState("");
  const [rpsLimit, setRpsLimit] = useState("");
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const load = useCallback(() => getJob(id), [id]);
  const { data: job, error, refresh } = usePolling(load);
  const loadErrors = useCallback(() => jobErrors(id), [id]);
  const { data: errors } = usePolling(loadErrors);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [retryNote, setRetryNote] = useState<string | null>(null);

  const retry = async () => {
    setRetryError(null);
    setRetryNote(null);
    try {
      const result = (await jobAction(id, "retry-errors")) as { items_requeued: number };
      setRetryNote(`Возвращено в работу элементов: ${count(result.items_requeued)}`);
      refresh();
    } catch (err) {
      setRetryError(errorText(err));
    }
  };

  const saveSettings = async (event: FormEvent) => {
    event.preventDefault();
    setSettingsError(null);
    const changes: { threads?: number; global_rate_limit_rps?: number } = {};
    if (threads) changes.threads = Number(threads);
    if (rpsLimit) changes.global_rate_limit_rps = Number(rpsLimit);
    try {
      await patchJob(id, changes);
      setThreads("");
      setRpsLimit("");
      refresh();
    } catch (err) {
      setSettingsError(errorText(err));
    }
  };

  const errorItems = job
    ? ERROR_ITEM_STATUSES.reduce((sum, status) => sum + (job.items[status] ?? 0), 0)
    : 0;

  return (
    <>
      <p>
        <a href="#/jobs">← Все задания</a>
      </p>
      <h1>Задание № {id}</h1>
      <ErrorMessage text={error} />
      {job && (
        <>
          <section className="card">
            <div className="job-head">
              <span>
                <StatusBadge status={job.status} />
                {job.finish_reason && <span className="muted-inline"> {job.finish_reason}</span>}
              </span>
              <JobActions job={job} onDone={refresh} />
            </div>
            <ProgressBar done={job.processed_ids} total={job.total_ids} />
            <p className="muted">
              Обработано {count(job.processed_ids)}
              {job.total_ids !== null && ` из ${count(job.total_ids)}`} идентификаторов
            </p>
          </section>
          <section className="card">
            <div className="job-head">
              <h2>Элементы</h2>
              <a href={`#/jobs/${id}/items`}>Все элементы, фильтры и экспорт</a>
            </div>
            <dl className="stats">
              {Object.entries(job.items)
                .filter(([, value]) => value > 0)
                .map(([status, value]) => (
                  <Stat key={status} label={statusLabel(status)} value={count(value)} />
                ))}
              {Object.values(job.items).every((value) => value === 0) && (
                <p className="muted">Результатов пока нет.</p>
              )}
            </dl>
          </section>
          {(errorItems > 0 || job.chunks_failed > 0) && (
            <section className="card">
              <div className="job-head">
                <h2>Ошибки</h2>
                {canOperate && (
                  <button type="button" className="button-quiet" onClick={() => void retry()}>
                    Повторить ошибочные
                  </button>
                )}
              </div>
              <ErrorMessage text={retryError} />
              {retryNote && <p className="muted">{retryNote}</p>}
              {job.chunks_failed > 0 && (
                <p className="muted">Блоков с исчерпанными попытками: {job.chunks_failed}</p>
              )}
              {errors && errors.length > 0 && (
                <div className="table-wrap table-flat">
                  <table>
                    <thead>
                      <tr>
                        <th>Группа</th>
                        <th>Класс ошибки</th>
                        <th>Статус</th>
                        <th className="num">Элементов</th>
                      </tr>
                    </thead>
                    <tbody>
                      {errors.map((row) => (
                        <tr key={`${row.status}-${row.error_class}`}>
                          <td>{row.error_group ?? "—"}</td>
                          <td className="mono">{row.error_class}</td>
                          <td>{statusLabel(row.status)}</td>
                          <td className="num">{count(row.count)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
          <section className="card">
            <h2>Блоки</h2>
            <dl className="stats">
              <Stat label="Всего" value={job.chunks_total === null ? "—" : count(job.chunks_total)} />
              <Stat label="Закрыто" value={count(job.chunks_done)} />
              <Stat label="В работе" value={count(job.chunks_leased)} />
              <Stat label="Размер блока" value={count(job.chunk_size)} />
            </dl>
          </section>
          <section className="card">
            <div className="job-head">
              <h2>Параметры</h2>
              <a href={`#/events/job/${id}`}>Журнал задания</a>
            </div>
            <dl className="stats">
              <Stat label="Диапазон" value={rangeText(job.range_from, job.range_to)} />
              <Stat label="Попытка запуска" value={count(job.run_attempt)} />
              <Stat label="Потоков на узел" value={job.threads === null ? "как у узла" : count(job.threads)} />
              <Stat
                label="Общий лимит, запросов/с"
                value={job.global_rate_limit_rps === null ? "нет" : count(job.global_rate_limit_rps)}
              />
              <Stat label="Тайм-аут запроса, с" value={count(job.request_timeout_seconds)} />
              <Stat label="Повторных попыток" value={count(job.max_retries)} />
              <Stat label="Создано" value={dateTime(job.created_at)} />
              <Stat label="Запущено" value={dateTime(job.started_at)} />
              <Stat label="Завершено" value={dateTime(job.finished_at)} />
            </dl>
            {canOperate && (
              <form className="job-form-fields container-update" onSubmit={saveSettings}>
                <label>
                  Потоков на узел
                  <input
                    type="number"
                    min={1}
                    value={threads}
                    onChange={(e) => setThreads(e.target.value)}
                  />
                </label>
                <label>
                  Общий лимит, запросов/с
                  <input
                    type="number"
                    min={0.1}
                    step="any"
                    value={rpsLimit}
                    onChange={(e) => setRpsLimit(e.target.value)}
                  />
                </label>
                <button type="submit" className="button-quiet" disabled={!threads && !rpsLimit}>
                  Изменить на ходу
                </button>
              </form>
            )}
            <ErrorMessage text={settingsError} />
          </section>
        </>
      )}
    </>
  );
}
