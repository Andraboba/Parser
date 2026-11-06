export type JobStatus =
  | "created"
  | "validating"
  | "running"
  | "pausing"
  | "paused"
  | "stopping"
  | "stopped"
  | "completed"
  | "failed";

export interface Job {
  id: number;
  range_from: number;
  range_to: number | null;
  chunk_size: number;
  status: JobStatus;
  finish_reason: string | null;
  run_attempt: number;
  threads: number | null;
  request_timeout_seconds: number;
  max_retries: number;
  global_rate_limit_rps: number | null;
  max_ids: number | null;
  max_duration_seconds: number | null;
  stop_on_consecutive_missing: number | null;
  workers: Record<string, number | null>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  total_ids: number | null;
  processed_ids: number;
  chunks_total: number | null;
  chunks_done: number;
  chunks_leased: number;
  chunks_failed: number;
  items: Record<string, number>;
}

export interface WorkerNode {
  id: number;
  name: string;
  provisioning_mode: string;
  container_id: string | null;
  image: string | null;
  cpu_limit: number | null;
  mem_limit_mb: number | null;
  threads: number;
  desired_threads: number | null;
  enabled: boolean;
  min_free_space_gb: number | null;
  rps_limit: number | null;
  storage_path: string | null;
  ssh_host: string | null;
  ssh_port: number | null;
  ssh_user: string | null;
  ssh_key_ref: string | null;
  agent_kind: string | null;
  desired_agent_state: string | null;
  status: string;
  agent_state: string | null;
  free_slots: number | null;
  lease_block_reason: string | null;
  agent_version: string;
  last_ip: string | null;
  last_heartbeat_at: string | null;
  leased_chunks: number;
  current_chunks: number[];
  metrics: Record<string, any> | null;
  counters: Record<string, number> | null;
  container_state: ContainerState | null;
}

export interface ContainerState {
  worker: string | null;
  id: string;
  image: string;
  state: string;
  restart_count: number;
  oom_killed: boolean;
  ssh?: string;
  error?: string;
}

export type AgentAction = "start" | "stop" | "restart";

export interface ServerCheck {
  reachable: boolean;
  installed: boolean;
  running: boolean;
  pid: number | null;
  release: string | null;
  python: string | null;
  kernel: string | null;
  error: string | null;
  error_code: string | null;
}

export interface NewServer {
  name: string;
  host: string | null;
  ssh_port: number;
  ssh_user: string;
  ssh_key: string;
  ssh_password?: string | null;
  threads: number;
  storage_path: string;
  min_free_space_gb: number | null;
  agent_kind: string;
  create_container: boolean;
  cpus: number;
  memory_mb: number;
}

export interface NewContainer {
  name: string;
  image: string;
  threads: number;
  cpus: number;
  memory_mb: number;
}

export interface WorkerPatch {
  desired_threads?: number | null;
  enabled?: boolean;
  min_free_space_gb?: number | null;
  rps_limit?: number | null;
}

export interface JobWorkerDraft {
  name: string;
  threads?: number;
}

export interface JobDraft {
  range: string;
  chunk_size: number;
  threads?: number;
  request_timeout_seconds?: number;
  max_retries?: number;
  global_rate_limit_rps?: number;
  max_ids?: number;
  max_duration_seconds?: number;
  stop_on_consecutive_missing?: number;
  workers?: JobWorkerDraft[];
}

export interface Item {
  external_id: number;
  status: string;
  error_class: string | null;
  error_group: string | null;
  http_status: number | null;
  attempts: number;
  worker: string | null;
  updated_at: string;
  title: string | null;
  page_url: string | null;
  download_url: string | null;
  mime: string | null;
  checksum: string | null;
  file_path: string | null;
  file_size: number | null;
}

export interface ItemsPage {
  items: Item[];
  next_after_id: number | null;
}

export interface ItemFilter {
  status?: string;
  worker?: string;
  q?: string;
}

export interface ErrorSummary {
  status: string;
  error_class: string;
  error_group: string | null;
  count: number;
}

export type JobAction = "start" | "pause" | "resume" | "stop" | "restart" | "retry-errors";

export class ApiError extends Error {
  status: number;
  code: string | null;

  constructor(status: number, message: string, code: string | null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** detail у FastAPI: строка из наших ошибок или список ошибок валидации. */
function describeError(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // Сообщения наших валидаторов pydantic начинает с технической приставки.
    const text = (d: { msg?: string }) => (d.msg ?? "ошибка").replace(/^Value error, /, "");
    return detail.map(text).join("; ");
  }
  return `Сервер ответил кодом ${status}`;
}

let csrfToken: string | null = null;
let onSessionLost: () => void = () => {};

export function setSessionLostHandler(handler: () => void): void {
  onSessionLost = handler;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  // Заголовок, который не может поставить чужой сайт: защита от CSRF (ТЗ, п. 14).
  if (init?.method && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(`/api${path}`, { ...init, headers });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const code = (body as { code?: string } | null)?.code ?? null;
    if (code === "not_authenticated") onSessionLost();
    throw new ApiError(response.status, describeError(response.status, body), code);
  }
  return body as T;
}

function post<T>(path: string, payload?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
}

function query(params: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

function patch<T>(path: string, payload: unknown): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export const createServer = (server: NewServer) => post<WorkerNode>("/servers", server);
export const checkServer = (id: number) => post<ServerCheck>(`/workers/${id}/agent/check`);
export const agentAction = (id: number, action: AgentAction) =>
  post<WorkerNode>(`/workers/${id}/agent/${action}`);
export const agentLogs = (id: number) =>
  request<{ logs: string }>(`/workers/${id}/agent/logs?tail=200`);
export const deployAgent = (id: number, kind: string) =>
  post<WorkerNode>(`/workers/${id}/agent/deploy`, { agent_kind: kind });
export const forgetHostKey = (id: number) => post<unknown>(`/workers/${id}/agent/forget-host-key`);

export const containerAction = (id: number, action: "start" | "stop" | "restart") =>
  post<ContainerState>(`/workers/${id}/container/${action}`);
export const containerLogs = (id: number) =>
  request<{ logs: string }>(`/workers/${id}/container/logs?tail=200`);
export const updateContainerImage = (id: number, image: string) =>
  post<ContainerState>(`/workers/${id}/container/update-image`, { image });
export const createContainer = (spec: NewContainer) => post<ContainerState>("/containers", spec);
export const allowedImages = () => request<string[]>("/containers/images");

export type Role = "viewer" | "operator" | "administrator";

export interface Me {
  login: string;
  role: Role;
  csrf_token: string;
}

export async function login(name: string, password: string): Promise<Me> {
  const me = await post<Me>("/auth/login", { login: name, password });
  csrfToken = me.csrf_token;
  return me;
}

export async function currentUser(): Promise<Me> {
  const me = await request<Me>("/auth/me");
  csrfToken = me.csrf_token;
  return me;
}

export async function logout(): Promise<void> {
  await post<null>("/auth/logout");
  csrfToken = null;
}

export interface UserRow {
  id: number;
  login: string;
  role: Role;
  disabled: boolean;
}

export const listUsers = () => request<UserRow[]>("/users");
export const createUser = (body: { login: string; password: string; role: Role }) =>
  post<UserRow>("/users", body);
export const patchUser = (
  id: number,
  changes: { role?: Role; password?: string; disabled?: boolean },
) => patch<UserRow>(`/users/${id}`, changes);

export interface AuditRow {
  id: number;
  at: string;
  actor: string;
  action: string;
  object: string | null;
  result: string;
  ip: string | null;
}

export interface AgentTokenRow {
  id: number;
  worker_name: string | null;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
}

export const listTokens = () => request<AgentTokenRow[]>("/agent-tokens");
export const issueToken = (workerName: string) =>
  post<AgentTokenRow & { token: string }>("/agent-tokens", { worker_name: workerName });
export const revokeToken = (id: number) => post<AgentTokenRow>(`/agent-tokens/${id}/revoke`);

export const listAudit = (filter: { actor?: string; action?: string }, beforeId?: number) =>
  request<AuditRow[]>(`/audit${query({ ...filter, before_id: beforeId })}`);

export interface EventRow {
  id: number;
  at: string;
  level: string;
  component: string;
  job_id: number | null;
  worker_id: number | null;
  chunk_id: number | null;
  external_id: number | null;
  operation: string;
  result: string;
  error_code: string | null;
  message: string | null;
}

export interface EventFilter {
  job_id?: string;
  worker_id?: string;
  external_id?: string;
  level?: string;
  result?: string;
  q?: string;
  since?: string;
  until?: string;
}

export const listEvents = (filter: EventFilter, beforeId?: number) =>
  request<EventRow[]>(`/events${query({ ...filter, before_id: beforeId })}`);

export interface SpeedSample {
  at: string;
  processed_ids: number;
  bytes_done: number;
}

export interface Dashboard {
  job: Job | null;
  bytes_done: number;
  ids_per_second: number;
  average_ids_per_second: number;
  bytes_per_second: number;
  average_bytes_per_second: number;
  eta_seconds: number | null;
  active_workers: number;
  active_threads: number;
  workers_total: number;
  samples: SpeedSample[];
}

export interface AlertRow {
  code: string;
  severity: string;
  message: string;
  worker_id: number | null;
  job_id: number | null;
}

export const getDashboard = (jobId?: number) =>
  request<Dashboard>(`/dashboard${query({ job_id: jobId })}`);
export const listAlerts = () => request<AlertRow[]>("/alerts");
export const patchJob = (
  id: number,
  changes: { threads?: number | null; global_rate_limit_rps?: number | null },
) => patch<Job>(`/jobs/${id}`, changes);

export const listJobs = () => request<Job[]>("/jobs");
export const getJob = (id: number) => request<Job>(`/jobs/${id}`);
export const createJob = (draft: JobDraft) => post<Job>("/jobs", draft);
export const jobAction = (id: number, action: JobAction) => post<unknown>(`/jobs/${id}/${action}`);
export const listWorkers = () => request<WorkerNode[]>("/workers");
export const getWorker = (id: number) => request<WorkerNode>(`/workers/${id}`);
export const patchWorker = (id: number, changes: WorkerPatch) =>
  patch<WorkerNode>(`/workers/${id}`, changes);
export const listItems = (id: number, filter: ItemFilter, afterId?: number) =>
  request<ItemsPage>(`/jobs/${id}/items${query({ ...filter, after_id: afterId, limit: 100 })}`);
export const itemsCsvUrl = (id: number, filter: ItemFilter) =>
  `/api/jobs/${id}/items.csv${query(filter)}`;
export const jobErrors = (id: number) => request<ErrorSummary[]>(`/jobs/${id}/errors`);

export interface Video {
  id: number;
  job_id: number;
  external_id: number;
  title: string | null;
  page_url: string | null;
  mime: string | null;
  size: number;
  file_name: string;
  worker: string | null;
  created_at: string;
  playable: boolean;
}

export interface VideosPage {
  items: Video[];
  total: number;
  next_before_id: number | null;
}

export interface VideoFilter {
  q?: string;
  job_id?: string;
  worker?: string;
}

export const listVideos = (filter: VideoFilter, beforeId?: number) =>
  request<VideosPage>(`/videos${query({ ...filter, before_id: beforeId, limit: 48 })}`);
export const videoUrl = (id: number, download = false) =>
  `/api/videos/${id}/content${download ? "?download=true" : ""}`;
export const authorizeServer = (id: number, password: string) =>
  post<{ ok: boolean }>(`/workers/${id}/agent/authorize`, { password });
