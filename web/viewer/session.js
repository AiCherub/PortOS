import { clearHints, flashHint, hideOverlay, showOverlayMessage } from "./feedback.js";

const RECOVER_KEY = "portos.recover";
const RECOVER_LIMIT = 3;
const REENTER = { label: "重新进入", href: "/" };

function forgetSession() {
  document.cookie = "portos_sid=; Path=/; Max-Age=0; SameSite=Lax";
}

// A session that has disappeared can be replaced a few times without
// trapping the visitor in a redirect loop if the service is broken.
function recoverOnce() {
  let tries = 0;
  try {
    tries = parseInt(sessionStorage.getItem(RECOVER_KEY) || "0", 10) || 0;
  } catch (_) {
    tries = 0;
  }
  if (tries >= RECOVER_LIMIT) return false;
  try {
    sessionStorage.setItem(RECOVER_KEY, String(tries + 1));
  } catch (_) { /* ignore */ }
  location.replace("/");
  return true;
}

export function createSession({ sid, control, display, audio, clipboard }) {
  let connected = false;
  let leaving = false;
  let endedOnPurpose = false;

  function stopMedia() {
    clipboard.stop();
    audio.stop();
  }

  function onConnect() {
    audio.start();
    connected = true;
    hideOverlay();
    flashHint("程序正在启动…", 6000);
    display.pushSize(true);
    setTimeout(() => {
      if (connected) flashHint("中文可以直接用你电脑上的输入法输入", 6000);
    }, 8000);
  }

  function scheduleRecovery() {
    // Give pagehide and the control channel's program-closed notice time
    // to arrive before requesting a replacement session.
    setTimeout(() => {
      if (leaving) return;
      if (endedOnPurpose) {
        hideOverlay();
        showOverlayMessage("本次会话已经结束", "程序已经关闭，运行环境也随之销毁。", REENTER);
        return;
      }

      flashHint("正在准备新的运行环境…", 3000);
      if (recoverOnce()) return;
      hideOverlay();
      showOverlayMessage("本次会话已经结束", "运行环境已经销毁，请重新进入。", REENTER);
    }, 900);
  }

  function onDisconnect() {
    stopMedia();
    connected = false;
    forgetSession();
    if (!leaving) scheduleRecovery();
  }

  function onConnectionError() {
    forgetSession();
    scheduleRecovery();
  }

  function onProgramClosed() {
    endedOnPurpose = true;
    stopMedia();
  }

  window.addEventListener("pagehide", () => {
    clearHints();
    stopMedia();
    leaving = true;
    // The server gives a refresh a short grace period to reconnect.
    try { control.send({ t: "bye" }); } catch (_) { /* ignore */ }
  });

  async function end(showEnding) {
    stopMedia();
    leaving = true;
    showEnding();

    try { control.send({ t: "quit" }); } catch (_) { /* ignore */ }
    try {
      await fetch(`/api/sessions/${sid}`, { method: "DELETE" });
    } catch (_) { /* ignore */ }

    forgetSession();
    display.disconnect();
    control.close();
    connected = false;
    document.body.classList.add("ended");
    showOverlayMessage("已退出程序", "这次会话已经结束，运行环境已被销毁。", REENTER);
  }

  return { onConnect, onDisconnect, onConnectionError, onProgramClosed, end };
}
