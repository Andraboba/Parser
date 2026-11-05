import { type FormEvent, useEffect, useState } from "react";

import { useAuth } from "../auth";
import { createJob, type JobDraft, listJobs, listWorkers, type WorkerNode } from "../api";
import { ErrorMessage, JobActions, ProgressBar, StatusBadge } from "../components";
import { count, rangeText } from "../format";
import { errorText, usePolling } from "../usePolling";

const OPTIONAL_FIELDS: { key: keyof JobDraft; label: string; hint: string; min: number }[] = [
  { key: "threads", label: "Потоков на узел", hint: "По умолчанию как у узла", min: 1 },
  { key: "global_rate_limit_rps", label: "Общий лимит, запросов/с", hint: "Без лимита", min: 0.1 },
  { key: "request_timeout_seconds", label: "Тайм-аут запроса, с", hint: "20", min: 0.1 },
  { key: "max_retries", label: "Повторных попыток", hint: "4", min: 0 },
  { key: "max_ids", label: "Предел идентификаторов", hint: "Без предела", min: 1 },
  { key: "max_duration_seconds", label: "Предел времени, с", hint: "Без предела", min: 1 },
  {
    key: "stop_on_consecutive_missing",
    label: "Стоп после N отсутствующих подряд",
    hint: "Отключено",
    min: 1,
  },
];

function CreateJobForm({ onCreated }: { onCreated: () => void }) {
  const [range, setRange] = useState("[1,10000]");
  const [chunkSize, setChunkSize] = useState("1000");
  const [optional, setOptional] = useState<Record<string, string>>({});
  const [workers, setWorkers] = useState<WorkerNode[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [workerThreads, setWorkerThreads] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listWorkers().then(setWorkers).catch(() => setWorkers([]));
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const draft: JobDraft = { range, chunk_size: Number(chunkSize) };
    for (const { key } of OPTIONAL_FIELDS) {
      const value = optional[key];
      if (value) Object.assign(draft, { [key]: Number(value) });
    }
    const chosen = workers
      .filter((worker) => selected[worker.name])
      .map((worker) => {
        const threads = workerThreads[worker.name];
        return threads ? { name: worker.name, threads: Number(threads) } : { name: worker.name };
      });
    if (chosen.length > 0) draft.workers = chosen;
    try {
      await createJob(draft);
      onCreated();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card job-form" onSubmit={submit}>
      <h2>Новое задание</h2>
      <div className="job-form-fields">
        <label className="field-wide">
          Диапазон
          <input
            value={range}
            onChange={(e) => setRange(e.target.value)}
            placeholder="[0,500000]"
            aria-describedby="range-hint"
            spellCheck={false}
          />
        </label>
        <label>
          Размер блока
          <input
            type="number"
            min={1}
            max={10000}
            required
            value={chunkSize}
            onChange={(e) => setChunkSize(e.target.value)}
          />
        </label>
        <button type="submit" disabled={busy}>
          Создать
        </button>
      </div>
      <p className="hint" id="range-hint">
        [A,B] — от A до B включительно. A — от 0 до A. [A,] — от A без верхней границы. Пустое поле
        — от 0 без верхней границы.
      </p>
      <details className="more-fields">
        <summary>Узлы и индивидуальные потоки</summary>
        {workers.length === 0 ? (
          <p className="hint">Узлы ещё не регистрировались. Задание будет доступно всем узлам.</p>
        ) : (
          <div className="job-form-fields">
            {workers.map((worker) => (
              <div key={worker.id} className="worker-choice">
                <label>
                  <input
                    type="checkbox"
                    checked={Boolean(selected[worker.name])}
                    onChange={(e) =>
                      setSelected({ ...selected, [worker.name]: e.target.checked })
                    }
                  />
                  {worker.name}
                </label>
                <input
                  type="number"
                  min={1}
                  placeholder="потоков"
                  aria-label={`Потоков для ${worker.name}`}
                  disabled={!selected[worker.name]}
                  value={workerThreads[worker.name] ?? ""}
                  onChange={(e) =>
                    setWorkerThreads({ ...workerThreads, [worker.name]: e.target.value })
                  }
                />
              </div>
            ))}
          </div>
        )}
        <p className="hint">
          Если не выбрать ни одного узла, блоки сможет брать любой зарегистрированный узел.
          Индивидуальное число потоков важнее общего значения задания.
        </p>
      </details>
      <details className="more-fields">
        <summary>Дополнительные параметры</summary>
        <div className="job-form-fields">
          {OPTIONAL_FIELDS.map(({ key, label, hint, min }) => (
            <label key={key}>
              {label}
              <input
                type="number"
                min={min}
                step="any"
                placeholder={hint}
                value={optional[key] ?? ""}
                onChange={(e) => setOptional({ ...optional, [key]: e.target.value })}
              />
            </label>
          ))}
        </div>
      </details>
      <ErrorMessage text={error} />
    </form>
  );
}

export function JobsPage() {
  const { canOperate } = useAuth();
  const { data: jobs, error, refresh } = usePolling(listJobs);

  return (
    <>
      <h1>Задания</h1>
      {canOperate && <CreateJobForm onCreated={refresh} />}
      <ErrorMessage text={error} />
      {jobs?.length === 0 && <p className="muted">Заданий пока нет.</p>}
      {jobs && jobs.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>№</th>
                <th>Диапазон</th>
                <th>Статус</th>
                <th className="col-progress">Прогресс</th>
                <th className="num">Обработано</th>
                <th className="num">Найдено</th>
                <th className="actions" aria-label="Действия" />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td>
                    <a href={`#/jobs/${job.id}`}>{job.id}</a>
                  </td>
                  <td className="nowrap">{rangeText(job.range_from, job.range_to)}</td>
                  <td>
                    <StatusBadge status={job.status} />
                  </td>
                  <td className="col-progress">
                    <ProgressBar done={job.processed_ids} total={job.total_ids} />
                  </td>
                  <td className="num">{count(job.processed_ids)}</td>
                  <td className="num">
                    {count((job.items.found ?? 0) + (job.items.completed ?? 0))}
                  </td>
                  <td className="actions">
                    <JobActions job={job} onDone={refresh} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
