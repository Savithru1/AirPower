import { createContext, useContext } from "react";
import { Me, Permission } from "./api";

// The signed-in user and what their role may do; views hide what the server would refuse.
export const AuthContext = createContext<Me | null>(null);

export function useAuth() {
  const me = useContext(AuthContext);
  const can = (p: Permission) => !!me?.user?.permissions.includes(p);
  return { me, user: me?.user ?? null, can };
}
