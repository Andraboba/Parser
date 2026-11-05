import { type FormEvent, useState } from "react";

import { useAuth } from "./auth";
import { containerAction, containerLogs, updateContainerImage, type WorkerNode } from "./api";
import { ErrorMessage } from "./components";
import { errorText } from "./usePolling";

/** Управление контейнером узла через runner. Без runner-а раздел не показывается. */
export function ContainerPanel({ worker, onDone }: { worker: WorkerNode; onDone: () => void }) {
  const { canOperate, isAdmin } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [logs, setLogs] = useState<string | null>(null);
  const [image, setImage] = useState("");

  const run = async (work: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await work();
      if (done) setNote(done);
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const state = worker.container_state;
  if (!state) return null;

  const showLogs = () =>
    run(async () => {
      const body = await containerLogs(worker.id);
      setLogs(body.logs);
    });

  const update = (event: FormEvent) => {
    event.preventDefault();
    if (!window.confirm(`Перевести ${worker.name} на образ ${image}? Текущий блок вернётся в очередь.`)) {
      return;
    }
    void run(() => updateContainerImage(worker.id, image), `Узел переведён на образ ${image}`);
  };

  const running = state.state === "running";
  return (
    <section className="card">
      <div className="job-head">
        <h2>Контейнер</h2>
        {canOperate && (
        <div className="actions-group">
          {running ? (
            <>
              <button
                type="button"
                className="button-quiet"
                disabled={busy}
                onClick={() => void run(() => containerAction(worker.id, "restart"))}
              >
                Перезапустить
              </button>
              <button
                type="button"
                className="button-quiet"
                disabled={busy}
                onClick={() => void run(() => containerAction(worker.id, "stop"))}
              >
                Остановить
              </button>
            </>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(() => containerAction(worker.id, "start"))}
            >
              Запустить
            </button>
          )}
          <button type="button" className="button-quiet" disabled={busy} onClick={() => void showLogs()}>
            Журнал
          </button>
        </div>
        )}
      </div>
      <dl className="stats">
        <div className="stat">
          <dt>Состояние</dt>
          <dd>{state.state}</dd>
        </div>
        <div className="stat">
          <dt>Образ</dt>
          <dd>{state.image}</dd>
        </div>
        <div className="stat">
          <dt>Перезапусков</dt>
          <dd>{state.restart_count}</dd>
        </div>
        <div className="stat">
          <dt>Убит по памяти</dt>
          <dd>{state.oom_killed ? "да" : "нет"}</dd>
        </div>
      </dl>
      {isAdmin && running && (
        <p className="hint">
          <a href={`#/workers/${worker.id}/terminal`}>Открыть терминал контейнера</a>
        </p>
      )}
      {isAdmin && (
      <form className="job-form-fields container-update" onSubmit={update}>
        <label>
          Новый образ
          <input
            required
            value={image}
            onChange={(e) => setImage(e.target.value)}
            placeholder="coord/agent-stub:v2"
            spellCheck={false}
          />
        </label>
        <button type="submit" className="button-quiet" disabled={busy}>
          Обновить образ
        </button>
      </form>
      )}
      <ErrorMessage text={error} />
      {note && <p className="muted">{note}</p>}
      {logs !== null && <pre className="logs">{logs || "Журнал пуст."}</pre>}
    </section>
  );
}
