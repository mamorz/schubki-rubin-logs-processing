import { X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { RoleId, StatusResponse } from "../shared/events";
import { api } from "./api";
import { ChatThread, Composer } from "./components/Chat";
import { EvidenceLedger, Header, RoleSwitcher, SignIn } from "./components/Panels";
import { useChat } from "./store";
import { useAgentSocket } from "./useAgentSocket";

export function App() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const role = useChat((s) => s.role);
  const setRole = useChat((s) => s.setRole);
  const connection = useChat((s) => s.connection);
  const ask = useChat((s) => s.ask);
  const notice = useChat((s) => s.notice);
  const dismissNotice = useChat((s) => s.dismissNotice);
  const busy = useChat((s) => {
    const last = s.turns[s.turns.length - 1];
    return Boolean(last && !last.done);
  });
  const loggedIn = status?.auth.loggedIn ?? false;
  const send = useAgentSocket(loggedIn, role);

  const refresh = useCallback(() => {
    api.status().then(setStatus).catch(() => undefined);
  }, []);

  useEffect(() => {
    refresh();
    // Poll quickly while a device login is pending, slowly otherwise.
    const timer = window.setInterval(refresh, status?.auth.pending ? 2000 : 15000);
    return () => window.clearInterval(timer);
  }, [refresh, status?.auth.pending]);

  const onAsk = (question: string) => {
    if (busy || !send({ type: "prompt", text: question })) return;
    ask(question);
  };

  const onRole = (next: RoleId) => {
    if (next !== role) setRole(next);
  };

  const onLogout = async () => {
    await api.logout();
    useChat.getState().clear();
    refresh();
  };

  return (
    <div className="flex h-full flex-col">
      <Header status={status} connection={connection} onLogout={onLogout} />
      {!loggedIn ? (
        <main className="flex-1 overflow-y-auto">
          <SignIn status={status} onRefresh={refresh} />
        </main>
      ) : (
        <main className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_22rem]">
          <section className="flex min-h-0 flex-col">
            <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-800">
              <div className="mx-auto max-w-3xl">
                <RoleSwitcher role={role} agents={status?.agents ?? []} onChange={onRole} />
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
              <div className="mx-auto max-w-3xl">
                <ChatThread onAsk={onAsk} />
              </div>
            </div>
            <div className="px-4 pb-4">
              <div className="mx-auto max-w-3xl space-y-2">
                {notice && (
                  <div className="flex items-start gap-2 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
                    <span className="flex-1">{notice}</span>
                    <button type="button" onClick={dismissNotice} aria-label="Dismiss"><X className="size-4" /></button>
                  </div>
                )}
                <Composer
                  disabled={connection === "connecting" || connection === "starting"}
                  busy={busy}
                  role={role}
                  onRole={onRole}
                  onAsk={onAsk}
                  onCancel={() => send({ type: "cancel" })}
                />
                <p className="text-center text-[11px] text-slate-400">
                  Counts are log records, not incidents. Team ownership is fictional. Answers separate measured facts from hypotheses.
                </p>
              </div>
            </div>
          </section>
          <div className="hidden min-h-0 border-l border-slate-200 p-4 lg:flex lg:flex-col dark:border-slate-800">
            <EvidenceLedger />
          </div>
        </main>
      )}
    </div>
  );
}
