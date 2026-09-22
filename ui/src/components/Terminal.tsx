import { useEffect, useRef } from "react";
import { Terminal as Xterm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

/**
 * §2.5's terminal plane, rendered.
 *
 * Bytes arrive raw, ANSI and all, and are written straight through: this is
 * the plane whose entire selling point is being byte-for-byte authentic, so
 * anything that "cleans up" the stream defeats it.
 *
 * The socket is **read-only by construction** -- xterm's own input is never
 * wired to it, and the daemon exposes no route that writes to a pane. Steering
 * an agent is Phase 5, and `tmux pipe-pane -I` (which writes into a pane "as
 * if typed") is a documented injection route the daemon refuses to use.
 */
export function TerminalPane({ agentId }: { agentId: string }) {
  const hostRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const term = new Xterm({
      convertEol: false,
      fontSize: 12,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      theme: { background: "#0b0d12", foreground: "#d6dbe5" },
      scrollback: 5000,
      disableStdin: true,
      cursorBlink: false,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();

    const resize = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* the pane can be measured mid-transition; the next tick is fine */
      }
    });
    resize.observe(host);

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(
      `${protocol}//${window.location.host}/ws/terminal/${encodeURIComponent(agentId)}`,
    );
    socket.binaryType = "arraybuffer";
    socket.onmessage = (message) => term.write(new Uint8Array(message.data));
    socket.onerror = () => term.writeln("\r\n\x1b[31m[pane: connection error]\x1b[0m");
    socket.onclose = () => term.writeln("\r\n\x1b[90m[pane: disconnected]\x1b[0m");

    return () => {
      socket.onclose = null;
      socket.close();
      resize.disconnect();
      term.dispose();
    };
  }, [agentId]);

  return <div className="terminal" ref={hostRef} />;
}
