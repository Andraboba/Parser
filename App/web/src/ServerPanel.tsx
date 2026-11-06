import { useState } from "react";

import {
  agentAction,
  authorizeServer,
  type AgentAction,
  agentLogs,
  checkServer,
  deployAgent,
  forgetHostKey,
  type ServerCheck,
  type WorkerNode,
} from "./api";
import { useAuth } from "./auth";
import { ErrorMessage } from "./components";
import { errorText } from "./usePolling";

const AGENT_STATES: Record<string, string> = {
  running: "Работает",
  starting: "Запускается",
  stopped: "Остановлен",
  not_installed: "Не установлен",
  unreachable: "Нет связи по SSH",
};

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

/** Сервер с агентом по SSH (ТЗ, п. 4): ключ здесь не виден, только имя секрета. */
export function ServerPanel({ worker, onDone }: { worker: WorkerNode; onDone: () => void }) {
  const { canOperate, isAdmin } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [logs, setLogs] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const state = worker.container_state;

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

  const check = () =>
    run(async () => {
      const result: ServerCheck = await checkServer(worker.id);
      setNote(
        result.reachable
          ? `Сервер доступен. ${result.python ?? ""}, ядро ${result.kernel ?? "?"}. Агент ${result.running ? `работает, pid ${result.pid}` : "не запущен"}.`
          : `Сервер недоступен: ${result.error}`,
      );
    });

  const action = (name: AgentAction) => run(() => agentAction(worker.id, name));

  const deploy = (kind: string) => {
    if (!window.confirm(`Установить агента (${kind}) на ${worker.name}? Он будет перезапущен.`)) return;
    void run(() => deployAgent(worker.id, kind), "Агент установлен и зарегистрировался.");
  };

  const running = state?.state === "running" || state?.state === "starting";
  const keyChanged = state?.ssh === "host_key_changed";
  return (
    <section className="card">
      <div className="job-head">
        <h2>Сервер и агент</h2>
        {canOperate && (
          <div className="actions-group">
            <button type="button" className="button-quiet" disabled={busy} onClick={() => void check()}>
              Проверить
            </button>
            {running ? (
              <>
                <button
                  type="button"
                  className="button-quiet"
                  disabled={busy}
                  onClick={() => void action("restart")}
                >
                  Перезапустить
                </button>
                <button
                  type="button"
                  className="button-quiet"
                  disabled={busy}
                  onClick={() => void action("stop")}
                >
                  Остановить
                </button>
              </>
            ) : (
              <button type="button" disabled={busy} onClick={() => void action("start")}>
                Запустить
              </button>
            )}
            <button
              type="button"
              className="button-quiet"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  setLogs((await agentLogs(worker.id)).logs);
                })
              }
            >
              Журнал
            </button>
          </div>
        )}
      </div>
      <dl className="stats">
        <Stat label="Адрес SSH" value={`${worker.ssh_host ?? "—"}:${worker.ssh_port ?? 22}`} />
        <Stat label="Пользователь" value={worker.ssh_user ?? "—"} />
        <Stat label="Ключ в хранилище" value={worker.ssh_key_ref ?? "—"} />
        <Stat label="Агент" value={AGENT_STATES[state?.state ?? ""] ?? state?.state ?? "Проверяется"} />
        <Stat label="Должен" value={worker.desired_agent_state === "running" ? "работать" : "быть остановлен"} />
        <Stat label="Вид агента" value={worker.agent_kind === "stub" ? "заглушка" : "настоящий"} />
        <Stat label="Выпуск" value={state?.image ?? "—"} />
        <Stat label="Перезапусков после сбоя" value={String(state?.restart_count ?? 0)} />
      </dl>
      {state?.error && <p className="error">{state.error}</p>}
      {isAdmin && (
        <div className="server-admin">
          <button type="button" className="button-quiet" disabled={busy} onClick={() => deploy("real")}>
            Установить настоящего агента
          </button>
          <button type="button" className="button-quiet" disabled={busy} onClick={() => deploy("stub")}>
            Установить заглушку
          </button>
          {keyChanged && (
            <button
              type="button"
              className="button-quiet"
              disabled={busy}
              onClick={() =>
                void run(() => forgetHostKey(worker.id), "Ключ забыт, новый запомнится при входе.")
              }
            >
              Сервер заменён: забыть его ключ
            </button>
          )}
          <form
            className="inline-form"
            onSubmit={(event) => {
              event.preventDefault();
              const value = password;
              void run(async () => {
                await authorizeServer(worker.id, value);
                setPassword("");
              }, "Ключ координатора установлен, дальше вход по ключу.");
            }}
          >
            <input
              type="password"
              autoComplete="new-password"
              required
              aria-label={`Пароль ${worker.ssh_user ?? "agent"}@${worker.ssh_host}`}
              placeholder={`Пароль ${worker.ssh_user ?? "agent"}`}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button type="submit" className="button-quiet" disabled={busy}>
              Войти по паролю и поставить ключ
            </button>
          </form>
          {state?.state !== "unreachable" && (
            <a href={`#/workers/${worker.id}/terminal`}>Открыть терминал сервера</a>
          )}
        </div>
      )}
      <ErrorMessage text={error} />
      {note && <p className="muted">{note}</p>}
      {logs !== null && <pre className="logs">{logs || "Журнал пуст."}</pre>}
    </section>
  );
}
