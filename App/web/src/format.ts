export function count(value: number): string {
  return value.toLocaleString("ru-RU");
}

export function dateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString("ru-RU") : "—";
}

/** Округляет вниз: незавершённое задание не должно показывать 100 %. */
export function percent(done: number, total: number): number {
  return total > 0 ? Math.floor((done / total) * 1000) / 10 : 0;
}

export function percentText(value: number): string {
  const digits = { minimumFractionDigits: 1, maximumFractionDigits: 1 };
  return `${value.toLocaleString("ru-RU", digits)} %`;
}

export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = { maximumFractionDigits: size < 10 && unit > 0 ? 1 : 0 };
  return `${size.toLocaleString("ru-RU", digits)} ${units[unit]}`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours > 0) return `${hours} ч ${minutes} мин`;
  if (minutes > 0) return `${minutes} мин ${total % 60} с`;
  return `${total} с`;
}

export const BLOCK_REASONS: Record<string, string> = {
  disabled: "узел отключён оператором",
  no_free_slots: "у узла нет свободных слотов",
  chunk_quota: "узел уже держит максимум блоков",
  disk_low: "мало свободного места",
  cpu_overload: "перегрузка CPU",
  memory_overload: "нехватка памяти",
  too_many_errors: "много ошибок за последнюю минуту",
};

export function rate(value: number): string {
  return value.toLocaleString("ru-RU", { maximumFractionDigits: value < 10 ? 1 : 0 });
}

export function rangeText(start: number, end: number | null): string {
  return end === null ? `от ${count(start)} без конца` : `${count(start)} – ${count(end)}`;
}

const ITEM_STATUS_LABELS: Record<string, string> = {
  pending: "Ожидает",
  checking: "Проверяется",
  not_found: "Не найдено",
  found: "Найдено",
  download_queued: "В очереди загрузки",
  downloading: "Загружается",
  completed: "Загружено",
  skipped: "Пропущено",
  forbidden: "Доступ запрещён",
  rate_limited: "Ограничение запросов",
  retry_wait: "Ждёт повтора",
  failed: "Ошибка",
  cancelled: "Отменено",
};

export const ITEM_STATUSES = Object.keys(ITEM_STATUS_LABELS);

export function statusLabel(status: string): string {
  return ITEM_STATUS_LABELS[status] ?? status;
}
