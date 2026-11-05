import { useCallback, useEffect, useState } from "react";

const POLL_INTERVAL_MS = 2000;

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * Опрашивает load каждые 2 с. Следующий запрос уходит только после завершения
 * предыдущего, поэтому медленный ответ не приводит к накоплению запросов.
 * При сбое последние данные остаются на экране вместе с текстом ошибки.
 * load должен быть стабильной ссылкой, иначе опрос перезапускается при каждом рендере.
 */
export function usePolling<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const value = await load();
        if (active) {
          setData(value);
          setError(null);
        }
      } catch (err) {
        if (active) setError(errorText(err));
      }
      if (active) timer = window.setTimeout(poll, POLL_INTERVAL_MS);
    };
    void poll();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [load, revision]);

  const refresh = useCallback(() => setRevision((r) => r + 1), []);
  return { data, error, refresh };
}
