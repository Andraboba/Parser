import { createContext, type ReactNode, useContext, useMemo } from "react";

import type { Me } from "./api";

interface AuthState {
  user: Me;
  canOperate: boolean;
  isAdmin: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ user, children }: { user: Me; children: ReactNode }) {
  const value = useMemo(
    () => ({
      user,
      canOperate: user.role !== "viewer",
      isAdmin: user.role === "administrator",
    }),
    [user],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Кнопки по роли только прячутся: настоящая проверка прав всегда на сервере. */
export function useAuth(): AuthState {
  const state = useContext(AuthContext);
  if (!state) throw new Error("useAuth вне AuthProvider");
  return state;
}
