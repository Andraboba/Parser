import { type FormEvent, useEffect, useState } from "react";

import { useAuth } from "../auth";
import { allowedImages, createContainer, listWorkers, type WorkerNode } from "../api";
import { ErrorMessage } from "../components";
import { BLOCK_REASONS, bytes, count, dateTime } from "../format";
import { errorText, usePolling } from "../usePolling";

const STATUS_LABELS: Record<string, string> = { online: "В сети", offline: "Нет связи" };

function limits(worker: WorkerNode): string {
  const cpu = worker.cpu_limit === null ? "без лимита" : `${count(worker.cpu_limit)} CPU`;
  const memory = worker.mem_limit_mb === null ? "без лимита" : `${count(worker.mem_limit_mb)} МБ`;
  return `${cpu}, ${memory}`;
}

function AddWorkerForm({ onCreated }: { onCreated: () => void }) {
  const [available, setAvailable] = useState(false);
  const [name, setName] = useState("");
  const [image, setImage] = useState("coord/agent-stub:dev");
  const [threads, setThreads] = useState("4");
  const [cpus, setCpus] = useState("0.5");
  const [memory, setMemory] = useState("256");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Без runner-а контейнерами управляет compose, и форма не нужна.
    allowedImages()
      .then(() => setAvailable(true))
      .catch(() => setAvailable(false));
  }, []);

  if (!available) return null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createContainer({
        name,
        image,
        threads: Number(threads),
        cpus: Number(cpus),
        memory_mb: Number(memory),
      });
      setName("");
      onCreated();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="card">
      <summary>Добавить узел</summary>
      <form className="job-form-fields add-worker" onSubmit={submit}>
        <label>
          Имя
          <input
            required
            pattern="[a-z0-9][a-z0-9\-]{0,62}"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="worker-04"
          />
        </label>
        <label>
          Образ
          <input required value={image} onChange={(e) => setImage(e.target.value)} />
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
          CPU
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
        <button type="submit" disabled={busy}>
          Создать
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
      {isAdmin && <AddWorkerForm onCreated={refresh} />}
      <ErrorMessage text={error} />
      {workers?.length === 0 && <p className="muted">Узлы ещё не регистрировались.</p>}
      {workers && workers.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Узел</th>
                <th>Статус</th>
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
                  <td className="nowrap">{worker.agent_version}</td>
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
