import { useState } from "react";

import { useAuth } from "./auth";
import { type Job, type JobAction, type JobStatus, jobAction } from "./api";
import { percent, percentText } from "./format";
import { errorText } from "./usePolling";

const STATUS_LABELS: Record<JobStatus, string> = {
  created: "Создано",
  validating: "Проверка",
  running: "Выполняется",
  pausing: "Ставится на паузу",
  paused: "На паузе",
  stopping: "Останавливается",
  stopped: "Остановлено",
  completed: "Завершено",
  failed: "Ошибка",
};

export function StatusBadge({ status }: { status: JobStatus }) {
  return <span className={`badge badge-${status}`}>{STATUS_LABELS[status]}</span>;
}

export function ProgressBar({ done, total }: { done: number; total: number | null }) {
  // Для открытого диапазона процента нет, оставшееся время неопределённо (ТЗ, п. 11.1).
  if (total === null) return <span className="muted-inline">диапазон без конца</span>;
  const value = percent(done, total);
  return (
    <div className="progress">
      <div
        className="progress-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(value)}
      >
        <div className="progress-fill" style={{ width: `${value}%` }} />
      </div>
      <span className="progress-label">{percentText(value)}</span>
    </div>
  );
}

export function ErrorMessage({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p className="error" role="alert">
      {text}
    </p>
  );
}

interface ActionSpec {
  action: JobAction;
  label: string;
  quiet?: boolean;
  confirm?: string;
}

const STOP: ActionSpec = {
  action: "stop",
  label: "Остановить",
  quiet: true,
  confirm: "Остановить задание? Текущие операции будут прерваны, контрольные точки сохранятся.",
};

const ACTIONS: Partial<Record<JobStatus, ActionSpec[]>> = {
  created: [{ action: "start", label: "Запустить" }],
  running: [{ action: "pause", label: "Пауза", quiet: true }, STOP],
  pausing: [{ action: "resume", label: "Продолжить" }, STOP],
  paused: [{ action: "resume", label: "Продолжить" }, STOP],
  stopped: [{ action: "restart", label: "Перезапустить" }],
  completed: [{ action: "restart", label: "Перезапустить", quiet: true }],
  failed: [{ action: "restart", label: "Перезапустить" }],
};

export function JobActions({ job, onDone }: { job: Job; onDone: () => void }) {
  const { canOperate } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const specs = canOperate ? (ACTIONS[job.status] ?? []) : [];

  const run = async (spec: ActionSpec) => {
    if (spec.confirm && !window.confirm(spec.confirm)) return;
    setBusy(true);
    setError(null);
    try {
      await jobAction(job.id, spec.action);
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="actions-group">
      {specs.map((spec) => (
        <button
          key={spec.action}
          type="button"
          className={spec.quiet ? "button-quiet" : undefined}
          disabled={busy}
          onClick={() => void run(spec)}
        >
          {spec.label}
        </button>
      ))}
      <ErrorMessage text={error} />
    </div>
  );
}
