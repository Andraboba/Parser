import { type FormEvent, useCallback, useEffect, useState } from "react";

import { useAuth } from "../auth";
import { getWorker, patchWorker, type WorkerPatch } from "../api";
import { ErrorMessage } from "../components";
import { ContainerPanel } from "../ContainerPanel";
import { ServerPanel } from "../ServerPanel";
import { BLOCK_REASONS, bytes, count, dateTime, duration } from "../format";
import { errorText, usePolling } from "../usePolling";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function ratio(used: number | undefined, limit: number | null | undefined): string {
  if (used === undefined || !limit) return "";
  return ` из ${limit < 1024 ? count(limit) : bytes(limit)} (${Math.round((used / limit) * 100)} %)`;
}

export function WorkerPage({ id }: { id: number }) {
  const { canOperate } = useAuth();
  const load = useCallback(() => getWorker(id), [id]);
  const { data: worker, error, refresh } = usePolling(load);
  const [threads, setThreads] = useState("");
  const [minFree, setMinFree] = useState("");
  const [rpsLimit, setRpsLimit] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const loadedName = worker?.name;

  useEffect(() => {
    if (!worker) return;
    setThreads(worker.desired_threads?.toString() ?? "");
    setMinFree(worker.min_free_space_gb?.toString() ?? "");
    setRpsLimit(worker.rps_limit?.toString() ?? "");
    // Поля формы заполняются один раз: опрос не должен затирать то, что вводит оператор.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedName]);

  const save = async (changes: WorkerPatch) => {
    setFormError(null);
    try {
      await patchWorker(id, changes);
      refresh();
    } catch (err) {
      setFormError(errorText(err));
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void save({
      desired_threads: threads ? Number(threads) : null,
      min_free_space_gb: minFree ? Number(minFree) : null,
      rps_limit: rpsLimit ? Number(rpsLimit) : null,
    });
  };

  const metrics = worker?.metrics ?? {};
  type CurrentItem = {
    id: number;
    title?: string | null;
    status?: string;
    download_bytes?: number;
    download_total?: number | null;
    download_bps?: number;
  };
  const currentItems = Array.isArray(metrics.current_items)
    ? (metrics.current_items as CurrentItem[])
    : [];
  const itemText = (item: CurrentItem): string => {
    const name = item.title ? `${item.id} — ${item.title}` : String(item.id);
    if (item.status !== "downloading" || item.download_bytes === undefined) return name;
    const total = item.download_total;
    const percent = total ? ` ${Math.round((item.download_bytes / total) * 100)} %` : "";
    const speed = item.download_bps ? `, ${bytes(item.download_bps)}/с` : "";
    return `${name}: ${bytes(item.download_bytes)}${total ? ` из ${bytes(total)}` : ""}${percent}${speed}`;
  };
  return (
    <>
      <p>
        <a href="#/workers">← Все узлы</a>
      </p>
      <h1>{worker?.name ?? `Узел № ${id}`}</h1>
      <ErrorMessage text={error} />
      {worker && (
        <>
          <section className="card">
            <div className="job-head">
              <span
                className={`badge ${worker.status === "online" ? "badge-online" : "badge-failed"}`}
              >
                {worker.status === "online" ? "В сети" : "Нет связи"}
              </span>
              {canOperate && (
                <button
                  type="button"
                  className="button-quiet"
                  onClick={() => void save({ enabled: !worker.enabled })}
                >
                  {worker.enabled ? "Отключить узел" : "Включить узел"}
                </button>
              )}
            </div>
            {worker.lease_block_reason && (
              <p className="error">
                Новые блоки не выдаются:{" "}
                {BLOCK_REASONS[worker.lease_block_reason] ?? worker.lease_block_reason}
              </p>
            )}
            <dl className="stats">
              <Stat label="Адрес" value={worker.last_ip ?? "—"} />
              <Stat label="Версия агента" value={worker.agent_version || "—"} />
              {worker.provisioning_mode !== "ssh" && (
                <>
                  <Stat label="Образ" value={worker.image ?? "—"} />
                  <Stat label="Контейнер" value={worker.container_id ?? "—"} />
                </>
              )}
              <Stat label="Режим" value={worker.provisioning_mode} />
              <Stat label="Heartbeat" value={dateTime(worker.last_heartbeat_at)} />
              <Stat label="Время работы" value={duration(metrics.uptime_seconds)} />
              <Stat label="Каталог файлов" value={worker.storage_path ?? "—"} />
            </dl>
            <p className="hint">
              <a href={`#/events/worker/${id}`}>Журнал узла</a>
            </p>
          </section>
          <section className="card">
            <h2>Работа</h2>
            <dl className="stats">
              <Stat label="Потоков" value={count(worker.desired_threads ?? worker.threads)} />
              <Stat label="Свободных слотов" value={worker.free_slots?.toString() ?? "—"} />
              <Stat
                label="Текущие блоки"
                value={worker.current_chunks.length ? worker.current_chunks.join(", ") : "нет"}
              />
              <Stat
                label="Текущие элементы"
                value={
                  currentItems.length
                    ? currentItems.map(itemText).join("; ")
                    : "—"
                }
              />
              <Stat label="Активных проверок" value={count(metrics.active_checks ?? 0)} />
              <Stat label="Активных загрузок" value={count(metrics.active_downloads ?? 0)} />
              <Stat label="Проверено" value={count(worker.counters?.checked ?? 0)} />
              <Stat label="Найдено" value={count(worker.counters?.found ?? 0)} />
              <Stat label="Ошибок" value={count(worker.counters?.failed ?? 0)} />
              <Stat label="Ответов 429" value={count(worker.counters?.rate_limited ?? 0)} />
            </dl>
          </section>
          <section className="card">
            <h2>Ресурсы</h2>
            <dl className="stats">
              <Stat
                label="CPU, ядер"
                value={
                  metrics.cpu_cores === undefined
                    ? "—"
                    : `${count(metrics.cpu_cores)}${ratio(metrics.cpu_cores, worker.cpu_limit)}`
                }
              />
              <Stat
                label="Память"
                value={
                  metrics.memory_bytes === undefined
                    ? "—"
                    : `${bytes(metrics.memory_bytes)}${ratio(metrics.memory_bytes, metrics.memory_limit)}`
                }
              />
              <Stat
                label="Диск свободно"
                value={
                  metrics.disk_free_bytes === undefined
                    ? "—"
                    : `${bytes(metrics.disk_free_bytes)} из ${bytes(metrics.disk_total_bytes)}`
                }
              />
              <Stat label="Swap" value={bytes(metrics.swap_used_bytes)} />
              <Stat label="Нагрузка (load1)" value={metrics.load1?.toString() ?? "—"} />
              <Stat label="Сеть, приём/с" value={bytes(metrics.net_rx_bps)} />
              <Stat label="Сеть, передача/с" value={bytes(metrics.net_tx_bps)} />
              <Stat label="Диск, чтение/с" value={bytes(metrics.disk_read_bps)} />
              <Stat label="Диск, запись/с" value={bytes(metrics.disk_write_bps)} />
              <Stat
                label="Открытых файлов"
                value={metrics.open_files == null ? "—" : count(metrics.open_files)}
              />
              <Stat
                label="Соединений"
                value={metrics.connections == null ? "—" : count(metrics.connections)}
              />
            </dl>
          </section>
          {worker.provisioning_mode === "ssh" ? (
            <ServerPanel worker={worker} onDone={refresh} />
          ) : (
            <ContainerPanel worker={worker} onDone={refresh} />
          )}
          {canOperate && (
          <form className="card" onSubmit={submit}>
            <h2>Настройки</h2>
            <div className="job-form-fields">
              <label>
                Потоков
                <input
                  type="number"
                  min={1}
                  placeholder={`Как заявил агент: ${worker.threads}`}
                  value={threads}
                  onChange={(e) => setThreads(e.target.value)}
                />
              </label>
              <label>
                Резерв места, ГБ
                <input
                  type="number"
                  min={0}
                  step="any"
                  placeholder="По умолчанию"
                  value={minFree}
                  onChange={(e) => setMinFree(e.target.value)}
                />
              </label>
              <label>
                Лимит узла, запросов/с
                <input
                  type="number"
                  min={0.1}
                  step="any"
                  placeholder="Без личного лимита"
                  value={rpsLimit}
                  onChange={(e) => setRpsLimit(e.target.value)}
                />
              </label>
              <button type="submit">Сохранить</button>
            </div>
            <ErrorMessage text={formError} />
          </form>
          )}
        </>
      )}
    </>
  );
}
