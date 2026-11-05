import { type FormEvent, useCallback, useState } from "react";

import {
  type AuditRow,
  createUser,
  issueToken,
  listAudit,
  listTokens,
  listUsers,
  patchUser,
  revokeToken,
  type Role,
} from "../api";
import { ErrorMessage } from "../components";
import { dateTime } from "../format";
import { errorText, usePolling } from "../usePolling";

const ROLE_LABELS: Record<Role, string> = {
  viewer: "Наблюдатель",
  operator: "Оператор",
  administrator: "Администратор",
};
const ROLES = Object.keys(ROLE_LABELS) as Role[];

export function UsersPage() {
  const { data: users, error, refresh } = usePolling(listUsers);
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [formError, setFormError] = useState<string | null>(null);

  const act = async (work: () => Promise<unknown>) => {
    setFormError(null);
    try {
      await work();
      refresh();
    } catch (err) {
      setFormError(errorText(err));
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void act(async () => {
      await createUser({ login, password, role });
      setLogin("");
      setPassword("");
    });
  };

  const changePassword = (id: number, name: string) => {
    const value = window.prompt(`Новый пароль для ${name}, не короче 10 символов`);
    if (value) void act(() => patchUser(id, { password: value }));
  };

  return (
    <>
      <h1>Пользователи</h1>
      <form className="card job-form-fields" onSubmit={submit}>
        <label>
          Логин
          <input
            required
            autoComplete="off"
            value={login}
            onChange={(e) => setLogin(e.target.value)}
          />
        </label>
        <label>
          Пароль
          <input
            required
            type="password"
            minLength={10}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label>
          Роль
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((value) => (
              <option key={value} value={value}>
                {ROLE_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">Создать</button>
      </form>
      <ErrorMessage text={formError ?? error} />
      {users && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Логин</th>
                <th>Роль</th>
                <th>Состояние</th>
                <th className="actions" aria-label="Действия" />
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id}>
                  <td>{user.login}</td>
                  <td>
                    <select
                      aria-label={`Роль ${user.login}`}
                      value={user.role}
                      onChange={(e) =>
                        void act(() => patchUser(user.id, { role: e.target.value as Role }))
                      }
                    >
                      {ROLES.map((value) => (
                        <option key={value} value={value}>
                          {ROLE_LABELS[value]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>{user.disabled ? "Отключён" : "Активен"}</td>
                  <td className="actions">
                    <div className="actions-group">
                      <button
                        type="button"
                        className="button-quiet"
                        onClick={() => changePassword(user.id, user.login)}
                      >
                        Сменить пароль
                      </button>
                      <button
                        type="button"
                        className="button-quiet"
                        onClick={() =>
                          void act(() => patchUser(user.id, { disabled: !user.disabled }))
                        }
                      >
                        {user.disabled ? "Включить" : "Отключить"}
                      </button>
                    </div>
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

export function TokensSection() {
  const { data: tokens, error, refresh } = usePolling(listTokens);
  const [worker, setWorker] = useState("");
  const [issued, setIssued] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setFormError(null);
    try {
      const row = await issueToken(worker);
      setIssued(row.token);
      setWorker("");
      refresh();
    } catch (err) {
      setFormError(errorText(err));
    }
  };

  const revoke = async (id: number) => {
    setFormError(null);
    try {
      await revokeToken(id);
      refresh();
    } catch (err) {
      setFormError(errorText(err));
    }
  };

  return (
    <>
      <h2 className="section-title">Токены агентов</h2>
      <p className="hint">
        Ротация: выпустите новый токен, передайте его агенту и перезапустите агента, затем отзовите
        старый. Пока оба действуют, узел работает без остановки.
      </p>
      <form className="card job-form-fields" onSubmit={(event) => void submit(event)}>
        <label>
          Узел
          <input
            required
            value={worker}
            onChange={(e) => setWorker(e.target.value)}
            placeholder="worker-01"
          />
        </label>
        <button type="submit">Выпустить токен</button>
      </form>
      {issued && (
        <p className="card">
          Токен показывается один раз, в базе остаётся только его хэш:{" "}
          <code className="mono">{issued}</code>
        </p>
      )}
      <ErrorMessage text={formError ?? error} />
      {tokens && tokens.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>№</th>
                <th>Узел</th>
                <th>Выпущен</th>
                <th>Действует до</th>
                <th>Отозван</th>
                <th className="actions" aria-label="Действия" />
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => (
                <tr key={token.id}>
                  <td>{token.id}</td>
                  <td>{token.worker_name ?? "все узлы"}</td>
                  <td className="nowrap">{dateTime(token.created_at)}</td>
                  <td className="nowrap">{dateTime(token.expires_at)}</td>
                  <td className="nowrap">{dateTime(token.revoked_at)}</td>
                  <td className="actions">
                    {!token.revoked_at && (
                      <button
                        type="button"
                        className="button-quiet"
                        onClick={() => void revoke(token.id)}
                      >
                        Отозвать
                      </button>
                    )}
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

export function AuditPage() {
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [filter, setFilter] = useState<{ actor?: string; action?: string }>({});
  const load = useCallback(() => listAudit(filter), [filter]);
  const { data, error } = usePolling(load);
  const [older, setOlder] = useState<AuditRow[]>([]);
  const rows = [...(data ?? []), ...older];

  const apply = (event: FormEvent) => {
    event.preventDefault();
    setOlder([]);
    setFilter({ actor: actor || undefined, action: action || undefined });
  };

  const more = async () => {
    const page = await listAudit(filter, rows[rows.length - 1]?.id);
    setOlder((current) => [...current, ...page]);
  };

  return (
    <>
      <h1>Журнал аудита</h1>
      <form className="card filters" onSubmit={apply}>
        <label>
          Кто
          <input value={actor} onChange={(e) => setActor(e.target.value)} />
        </label>
        <label>
          Действие
          <input value={action} onChange={(e) => setAction(e.target.value)} />
        </label>
        <button type="submit">Найти</button>
      </form>
      <ErrorMessage text={error} />
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Время</th>
                <th>Кто</th>
                <th>Действие</th>
                <th>Объект</th>
                <th className="num">Результат</th>
                <th>Адрес</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="nowrap">{dateTime(row.at)}</td>
                  <td>{row.actor}</td>
                  <td className="mono nowrap">{row.action}</td>
                  <td>{row.object ?? "—"}</td>
                  <td className="num">{row.result}</td>
                  <td>{row.ip ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rows.length >= 100 && (
        <p>
          <button type="button" className="button-quiet" onClick={() => void more()}>
            Показать ещё
          </button>
        </p>
      )}
    </>
  );
}
