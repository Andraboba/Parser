import { lazy, Suspense, useEffect, useState } from "react";

import { currentUser, logout, type Me, setSessionLostHandler } from "./api";
import { AuthProvider, useAuth } from "./auth";
import { AuditPage, TokensSection, UsersPage } from "./pages/AdminPages";
import { DashboardPage } from "./pages/DashboardPage";
import { EventsPage } from "./pages/EventsPage";
import { GalleryPage } from "./pages/GalleryPage";
import { ItemsPage } from "./pages/ItemsPage";
import { JobPage } from "./pages/JobPage";
import { JobsPage } from "./pages/JobsPage";
import { LoginPage } from "./pages/LoginPage";
import { WorkerPage } from "./pages/WorkerPage";
import { WorkersPage } from "./pages/WorkersPage";

const TerminalPage = lazy(() => import("./pages/TerminalPage"));

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const update = () => setHash(window.location.hash);
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return hash;
}

function Page({ hash }: { hash: string }) {
  const { isAdmin } = useAuth();
  let match: RegExpExecArray | null;
  if ((match = /^#\/workers\/(\d+)\/terminal$/.exec(hash)) && isAdmin) {
    return (
      <Suspense fallback={<p className="muted">Загрузка…</p>}>
        <TerminalPage key={match[1]} id={Number(match[1])} />
      </Suspense>
    );
  }
  if ((match = /^#\/workers\/(\d+)$/.exec(hash))) {
    return <WorkerPage key={match[1]} id={Number(match[1])} />;
  }
  if (hash === "#/workers") return <WorkersPage />;
  if ((match = /^#\/jobs\/(\d+)\/items$/.exec(hash))) {
    return <ItemsPage key={match[1]} jobId={Number(match[1])} />;
  }
  if ((match = /^#\/jobs\/(\d+)$/.exec(hash))) {
    return <JobPage key={match[1]} id={Number(match[1])} />;
  }
  if (hash === "#/jobs") return <JobsPage />;
  if (hash === "#/gallery") return <GalleryPage />;
  if ((match = /^#\/events\/job\/(\d+)$/.exec(hash))) {
    return <EventsPage key={hash} preset={{ job_id: match[1] }} />;
  }
  if ((match = /^#\/events\/worker\/(\d+)$/.exec(hash))) {
    return <EventsPage key={hash} preset={{ worker_id: match[1] }} />;
  }
  if (hash === "#/events") return <EventsPage key={hash} preset={{}} />;
  if (hash === "#/audit" && isAdmin) return <AuditPage />;
  if (hash === "#/users" && isAdmin) {
    return (
      <>
        <UsersPage />
        <TokensSection />
      </>
    );
  }
  return <DashboardPage />;
}

function NavLink({ href, active, label }: { href: string; active: boolean; label: string }) {
  return (
    <a href={href} aria-current={active ? "page" : undefined}>
      {label}
    </a>
  );
}

function Shell({ user, onLogout }: { user: Me; onLogout: () => void }) {
  const hash = useHash();
  const { isAdmin } = useAuth();
  const section = hash.split("/")[1] ?? "";

  return (
    <>
      <header className="topbar">
        <span className="brand">Координатор</span>
        <nav aria-label="Разделы">
          <NavLink href="#/" active={!section} label="Панель" />
          <NavLink href="#/jobs" active={section === "jobs"} label="Задания" />
          <NavLink href="#/gallery" active={section === "gallery"} label="Галерея" />
          <NavLink href="#/workers" active={section === "workers"} label="Узлы" />
          <NavLink href="#/events" active={section === "events"} label="Журнал" />
          {isAdmin && <NavLink href="#/audit" active={section === "audit"} label="Аудит" />}
          {isAdmin && <NavLink href="#/users" active={section === "users"} label="Пользователи" />}
        </nav>
        <div className="topbar-user">
          <span>{user.login}</span>
          <button type="button" className="button-quiet" onClick={onLogout}>
            Выйти
          </button>
        </div>
      </header>
      <main>
        <Page hash={hash} />
      </main>
    </>
  );
}

export function App() {
  const [user, setUser] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    setSessionLostHandler(() => setUser(null));
    currentUser()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setChecked(true));
  }, []);

  if (!checked) return null;
  if (!user) return <LoginPage onLogin={setUser} />;

  const leave = () => {
    void logout().finally(() => setUser(null));
  };
  return (
    <AuthProvider user={user}>
      <Shell user={user} onLogout={leave} />
    </AuthProvider>
  );
}
