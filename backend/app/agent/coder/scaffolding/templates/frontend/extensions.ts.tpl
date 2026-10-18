import { request } from "./api";

export type Mutation = {
  entity: string;
  action: "created" | "updated" | "deleted";
  id: number;
};
export type AppContext = {
  // Auth can wrap this once; every CRUD request uses the same transport.
  request: typeof request;
  invalidate: () => Promise<void>;
  mutations: Array<(event: Mutation) => void | Promise<void>>;
};
export type Plugin = (context: AppContext) => void | (() => void);

export function createContext(transport = request): AppContext {
  return { request: transport, invalidate: async () => {}, mutations: [] };
}

export function installPlugins(context: AppContext, plugins: Plugin[]) {
  const cleanup: Array<void | (() => void)> = [];
  let closed = false;
  const dispose = () => {
    if (closed) return;
    closed = true;
    for (const close of cleanup.reverse()) {
      try {
        close?.();
      } catch (error) {
        console.warn("Extension shutdown failed", error);
      }
    }
  };
  try {
    for (const install of plugins) cleanup.push(install(context));
  } catch (error) {
    dispose();
    throw error;
  }
  return dispose;
}
