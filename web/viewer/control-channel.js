export function createControlChannel(base) {
  const encoder = new TextEncoder();
  let socket = null;
  let heartbeat;
  let retry;
  let stopped = false;

  function send(message) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(encoder.encode(JSON.stringify(message) + "\n"));
    return true;
  }

  function connect(handlers) {
    const { onOpen, onReady, onMessage, onClose } = handlers;
    // websockify forwards binary frames. The messages inside are newline-delimited JSON.
    const connection = new WebSocket(`${base}/ctl?token=ctl`, ["binary"]);
    socket = connection;
    connection.binaryType = "arraybuffer";
    const decoder = new TextDecoder();
    let buffer = "";

    connection.addEventListener("open", () => {
      send({ t: "hello" });
      onOpen();
      send({ t: "ping" });
      heartbeat = setInterval(() => send({ t: "ping" }), 5000);
      onReady();
    });

    connection.addEventListener("message", (event) => {
      buffer += typeof event.data === "string"
        ? event.data
        : decoder.decode(event.data, { stream: true });
      if (buffer.length > 1048576) {
        connection.close();
        return;
      }
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        let message;
        try { message = JSON.parse(line); } catch (_) { continue; }
        onMessage(message);
      }
    });

    connection.addEventListener("close", () => {
      clearInterval(heartbeat);
      socket = null;
      onClose();
      if (!stopped) {
        retry = setTimeout(() => connect(handlers), 2000);
      }
    });

    connection.addEventListener("error", () => {
      try { connection.close(); } catch (_) { /* ignore */ }
    });
  }

  function close() {
    stopped = true;
    clearTimeout(retry);
    clearInterval(heartbeat);
    try { socket?.close(); } catch (_) { /* ignore */ }
  }

  return { connect, send, close };
}
