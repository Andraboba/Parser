import { type FormEvent, useState } from "react";

import { login, type Me } from "../api";
import { ErrorMessage } from "../components";
import { errorText } from "../usePolling";

export function LoginPage({ onLogin }: { onLogin: (user: Me) => void }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await login(name, password));
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <form className="card login-form" onSubmit={submit}>
        <h1>Координатор</h1>
        <label>
          Логин
          <input
            required
            autoFocus
            autoComplete="username"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          Пароль
          <input
            required
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <button type="submit" disabled={busy}>
          Войти
        </button>
        <ErrorMessage text={error} />
      </form>
    </main>
  );
}
