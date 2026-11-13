import { audioEnabled, clipboardMode } from "../js/session-config.js";
import { SessionAudio } from "./audio.js";
import { SessionClipboard } from "./clipboard.js";
import { createControlChannel } from "./control-channel.js";
import { flashHint } from "./feedback.js";
import { createInput } from "./input.js";
import { createRemoteDisplay } from "./remote-display.js";
import { createSession } from "./session.js";
import { createToolbar } from "./toolbar.js";

const screen = document.getElementById("screen");
const isTouch = (navigator.maxTouchPoints || 0) > 0 || "ontouchstart" in window;
if (isTouch) document.body.classList.add("touch");

const sid = location.pathname.match(/^\/s\/([0-9a-f]{32})/)?.[1] || "";
const protocol = location.protocol === "https:" ? "wss:" : "ws:";
// Direct connections use the origin. The gateway proxies through the session prefix.
const base = `${protocol}//${location.host}${sid ? `/s/${sid}` : ""}`;
const control = createControlChannel(base);
const audio = new SessionAudio(base, { enabled: audioEnabled });
const clipboard = new SessionClipboard(control.send, flashHint, clipboardMode);
const display = createRemoteDisplay({ base, screen, isTouch, send: control.send });
const input = createInput({
  screen, isTouch, getCanvas: display.getCanvas, send: control.send,
  dismissToolbar: () => toolbar.dismiss(),
});
const session = createSession({ sid, control, display, audio, clipboard });
const toolbar = createToolbar({
  isTouch, input, send: control.send, onEnd: () => session.end(toolbar.showEnding),
});

function handleControlMessage(message) {
  clipboard.receive(message);
  if (message.t === "size" && message.ok === false) {
    flashHint("分辨率已调整为允许的最大值", 3000);
  } else if (message.t === "text" && message.ok === false) {
    flashHint("这段中文没有送进程序，请再试一次", 4000);
  } else if (message.t === "windows") {
    toolbar.renderWindows(Array.isArray(message.windows) ? message.windows : []);
  } else if (message.t === "cursor") {
    display.setPointerShape(message.shape);
  } else if (message.t === "hello") {
    if (Array.isArray(message.windows)) toolbar.renderWindows(message.windows);
    display.setPointerShape(message.cursor);
  } else if (message.t === "ended") {
    session.onProgramClosed();
    toolbar.renderWindows([]);
    flashHint("程序已经关闭", 4000);
  }
}

display.connect({
  onConnect() {
    session.onConnect();
    if (!isTouch) input.focusIME();
  },
  onDisconnect: session.onDisconnect,
  onError: session.onConnectionError,
  onBell: () => flashHint("程序发出了提示音"),
});

control.connect({
  onOpen: () => clipboard.connect(),
  onReady() {
    display.pushSize(true);
    input.flushPendingText();
    toolbar.startPolling();
  },
  onMessage: handleControlMessage,
  onClose() {
    clipboard.disconnect();
    toolbar.stopPolling();
  },
});
