import { useCallback, useEffect, useRef } from "react";
import type { ClientMessage, RoleId, ServerMessage } from "../shared/events";
import { useChat } from "./store";

/** Keeps one WebSocket to the agent bridge while signed in; reconnects with backoff. */
export function useAgentSocket(enabled: boolean, role: RoleId) {
  const socketRef = useRef<WebSocket | null>(null);
  const roleRef = useRef(role);
  const receive = useChat((s) => s.receive);
  const setConnection = useChat((s) => s.setConnection);

  const send = useCallback((message: ClientMessage) => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
      return true;
    }
    return false;
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let retry = 0;
    let timer: number | undefined;

    const connect = () => {
      setConnection("connecting");
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${protocol}://${location.host}/api/session`);
      socketRef.current = socket;
      socket.onopen = () => {
        retry = 0;
        socket.send(JSON.stringify({ type: "role", role: roleRef.current } satisfies ClientMessage));
      };
      socket.onmessage = (event) => receive(JSON.parse(String(event.data)) as ServerMessage);
      socket.onclose = () => {
        setConnection("closed");
        if (stopped) return;
        timer = window.setTimeout(connect, Math.min(10_000, 500 * 2 ** retry++));
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [enabled, receive, setConnection]);

  useEffect(() => {
    if (roleRef.current === role) return;
    roleRef.current = role;
    send({ type: "role", role });
  }, [role, send]);

  return send;
}
