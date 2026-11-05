import "@xterm/xterm/css/xterm.css";

import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { useEffect, useRef, useState } from "react";

const CLOSE_REASONS: Record<number, string> = {
  4401: "Нужно войти в систему.",
  4403: "Терминал доступен только администратору.",
  4404: "Узел не найден.",
};

/** Загружается отдельно: xterm нужен только администратору и только на этой странице. */
export default function TerminalPage({ id }: { id: number }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState("Подключение…");

  useEffect(() => {
    if (!host.current) return;
    const term = new Terminal({ cursorBlink: true, fontSize: 13, convertEol: false });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host.current);
    fit.fit();

    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${window.location.host}/api/workers/${id}/terminal`);
    socket.binaryType = "arraybuffer";
    const encoder = new TextEncoder();

    const sendSize = () => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ resize: [term.cols, term.rows] }));
      }
    };
    socket.onopen = () => {
      setState("Сессия открыта. Действия записываются в журнал аудита.");
      sendSize();
      term.focus();
    };
    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        term.writeln(`\r\n\x1b[33m${event.data}\x1b[0m`);
      } else {
        term.write(new Uint8Array(event.data as ArrayBuffer));
      }
    };
    socket.onclose = (event) => {
      setState(CLOSE_REASONS[event.code] ?? "Сессия закрыта.");
    };
    const input = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(encoder.encode(data));
    });
    const onResize = () => {
      fit.fit();
      sendSize();
    };
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      input.dispose();
      socket.close();
      term.dispose();
    };
  }, [id]);

  return (
    <>
      <p>
        <a href={`#/workers/${id}`}>← Узел</a>
      </p>
      <h1>Терминал</h1>
      <p className="muted" role="status">
        {state}
      </p>
      <div className="terminal" ref={host} />
    </>
  );
}
