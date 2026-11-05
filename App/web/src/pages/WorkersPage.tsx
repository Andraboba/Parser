import { type FormEvent, useState } from "react";

import { useAuth } from "../auth";
import { createServer, listWorkers, type WorkerNode } from "../api";
import { ErrorMessage } from "../components";
import { BLOCK_REASONS, bytes, count, dateTime } from "../format";
import { errorText, usePolling } from "../usePolling";

const STATUS_LABELS: Record<string, string> = { online: "В сети", offline: "Нет связи" };

function limits(worker: WorkerNode): string {
  const cpu = worker.cpu_limit === null ? "без лимита" : `${count(worker.cpu_limit)} CPU`;
  const memory = worker.mem_limit_mb === null ? "без лимита" : `${count(worker.mem_limit_mb)} МБ`;
  return `${cpu}, ${memory}`;
}

function AddServerForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [newContainer, setNewContainer] = useState(true);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("22");
  const [user, setUser] = useState("agent");
  const [key, setKey] = useState("ssh_client_key");
  const [threads, setThreads] = useState("4");
  const [storage, setStorage] = useState("/data/videos");
  const [reserve, setReserve] = useState("");
  const [kind, setKind] = useState("real");
  const [cpus, setCpus] = useState("0.5");
  const [memory, setMemory] = useState("256");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createServer({
        name,
        host: newContainer ? null : host,
        ssh_port: Number(port),
        ssh_user: user,
        ssh_key: key,
        threads: Number(threads),
        storage_path: storage,
        min_free_space_gb: reserve ? Number(reserve) : null,
        agent_kind: kind,
        create_container: newContainer,
        cpus: Number(cpus),
        memory_mb: Number(memory),
      });
      setName("");
      setHost("");
      onCreated();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="card">
      <summary>Добавить сервер</summary>
      <p className="hint">
        Координатор сам войдёт на сервер по SSH, установит и запустит агента. На стенде сервером
        становится новый контейнер, для настоящей машины снимите отметку и укажите адрес.
      </p>
      <form className="job-form-fields add-worker" onSubmit={submit}>
        <label>
          Имя
          <input
            required
            pattern="[a-z0-9][a-z0-9\\-]{0,62}"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="worker-04"
          />
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={newContainer}
            onChange={(e) => setNewContainer(e.target.checked)}
          />
          Создать контейнер
        </label>
        {newContainer ? (
          <>
            <label>
              CPU контейнера
              <input
                type="number"
                min={0.1}
                step="any"
                required
                value={cpus}
                onChange={(e) => setCpus(e.target.value)}
              />
            </label>
            <label>
              Память, МБ
              <input
                type="number"
                min={64}
                required
                value={memory}
                onChange={(e) => setMemory(e.target.value)}
              />
            </label>
          </>
        ) : (
          <>
            <label>
              Адрес
              <input required value={host} onChange={(e) => setHost(e.target.value)} />
            </label>
            <label>
              Порт SSH
              <input
                type="number"
                min={1}
                max={65535}
                required
                value={port}
                onChange={(e) => setPort(e.target.value)}
              />
            </label>
          </>
        )}
        <label>
          Пользователь
          <input required value={user} onChange={(e) => setUser(e.target.value)} />
        </label>
        <label>
          Ключ (имя секрета)
          <input required value={key} onChange={(e) => setKey(e.target.value)} />
        </label>
        <label>
          Потоков
          <input
            type="number"
            min={1}
            required
            value={threads}
            onChange={(e) => setThreads(e.target.value)}
          />
        </label>
        <label>
          Каталог файлов
          <input required value={storage} onChange={(e) => setStorage(e.target.value)} />
        </label>
        <label>
          Резерв места, ГБ
          <input
            type="number"
            min={0}
            step="any"
            placeholder="По умолчанию"
            value={reserve}
            onChange={(e) => setReserve(e.target.value)}
          />
        </label>
        <label>
          Агент
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="real">Настоящий</option>
            <option value="stub">Заглушка</option>
          </select>
        </label>
        <button type="submit" disabled={busy}>
          Добавить
        </button>
      </form>
      <ErrorMessage text={error} />
    </details>
  );
}

export function WorkersPage() {
  const { isAdmin } = useAuth();
  const { data: workers, error, refresh } = usePolling(listWorkers);

  return (
    <>
      <h1>Узлы</h1>
      {isAdmin && <AddServerForm onCreated={refresh} />}
      <ErrorMessage text={error} />
      {workers?.length === 0 && <p className="muted">Узлы ещё не регистрировались.</p>}
      {workers && workers.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Узел</th>
                <th>Статус</th>
                <th>Адрес</th>
                <th className="num">Потоки</th>
                <th>Лимиты</th>
                <th className="num">Блоков в работе</th>
                <th className="num">Свободно на диске</th>
                <th>Версия агента</th>
                <th>Heartbeat</th>
              </tr>
            </thead>
            <tbody>
              {workers.map((worker) => (
                <tr key={worker.id}>
                  <td className="nowrap">
                    <a href={`#/workers/${worker.id}`}>{worker.name}</a>
                  </td>
                  <td>
                    <span
                      className={`badge ${worker.status === "online" ? "badge-online" : "badge-failed"}`}
                    >
                      {STATUS_LABELS[worker.status] ?? worker.status}
                    </span>
                  </td>
                  <td className="nowrap mono">
                    {worker.ssh_host ?? "—"}
                    {worker.ssh_host && worker.ssh_port !== 22 && `:${worker.ssh_port}`}
                  </td>
                  <td className="num">{worker.desired_threads ?? worker.threads}</td>
                  <td className="nowrap">{limits(worker)}</td>
                  <td className="num">
                    {worker.lease_block_reason ? (
                      <span
                        className="muted-inline"
                        title={BLOCK_REASONS[worker.lease_block_reason] ?? worker.lease_block_reason}
                      >
                        не выдаются
                      </span>
                    ) : (
                      worker.leased_chunks
                    )}
                  </td>
                  <td className="num">{bytes(worker.metrics?.disk_free_bytes)}</td>
                  <td className="nowrap">{worker.agent_version || "—"}</td>
                  <td className="nowrap">{dateTime(worker.last_heartbeat_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
