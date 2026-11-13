import RFB from "../core/rfb.js";

/*
 * The shape of the pointer.
 *
 * The picture carries no pointer - the session runs its X server with
 * -nocursor, because a pointer that is drawn into the picture is always
 * one round trip behind the mouse. What the visitor sees instead is the
 * pointer of their own machine, and the session tells us which shape the
 * remote pointer has: an I-beam over a text field, a resize arrow over a
 * window border. These are the CSS that go with those shapes.
 */
const POINTER_SHAPES = {
  default: "default",
  text: "text",
  move: "move",
  "ew-resize": "ew-resize",
  "ns-resize": "ns-resize",
  "nwse-resize": "nwse-resize",
  "nesw-resize": "nesw-resize",
};

export function createRemoteDisplay({ base, screen, isTouch, send }) {
  const stageEl = document.getElementById("stage");
  let rfb = null;
  let resizeTimer = null;
  let lastSent = { w: 0, h: 0 };

  /* The canvas noVNC draws on - the element its keyboard handler listens to. */
  function getCanvas() {
    return screen.querySelector("canvas");
  }

  function setPointerShape(shape) {
    const canvas = getCanvas();
    if (!canvas) return;
    const value = POINTER_SHAPES[shape] || "default";
    // The style sheet hands the canvas a pointer as well, so this has to
    // outrank it.
    canvas.style.setProperty("cursor", value, "important");
  }

  function connect({ onConnect, onDisconnect, onError, onBell }) {
    // One websockify serves both channels; the token says which one.
    const url = `${base}/websockify?token=vnc`;
    try {
      rfb = new RFB(screen, url, { credentials: { password: "" } });
    } catch (err) {
      // Older/newer noVNC builds differ in the constructor signature.
      rfb = new RFB(screen, { url: url, credentials: { password: "" } });
    }

    rfb.scaleViewport = true;
    rfb.clipViewport = false;
    rfb.viewOnly = false;
    // The canvas must not take the focus: the hidden input field needs it, or
    // the input method on the visitor's machine stops talking to the page.
    rfb.focusOnClick = false;

    rfb.addEventListener("connect", onConnect);
    rfb.addEventListener("disconnect", onDisconnect);
    rfb.addEventListener("securityfailure", onError);
    rfb.addEventListener("credentialsrequired", onError);
    rfb.addEventListener("bell", onBell);
  }

  function disconnect() {
    if (rfb) {
      try { rfb.disconnect(); } catch (_) { /* ignore */ }
    }
  }

  function currentSize() {
    // The picture area is the stage. The bar at the bottom belongs to the
    // browser, so the remote screen is sized to what is really visible - that
    // way nothing of the program is hidden behind the bar.
    const rect = stageEl.getBoundingClientRect();
    return {
      w: Math.max(320, Math.floor(rect.width)),
      h: Math.max(240, Math.floor(rect.height)),
    };
  }

  function pushSize(force) {
    const { w, h } = currentSize();
    if (!force && w === lastSent.w && h === lastSent.h) return;
    if (send({ t: "size", w, h })) lastSent = { w, h };
  }

  /*
   * A soft keyboard that comes up shrinks the visible area, but not the
   * window. That must not resize the remote screen - the program would jump
   * around every time the keyboard comes up or goes away.
   */
  function keyboardIsUp() {
    if (!isTouch) return false;
    const view = window.visualViewport;
    if (!view) return false;
    return view.height < window.innerHeight - 40;
  }

  function scheduleResize() {
    if (keyboardIsUp()) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => pushSize(false), 250);
  }

  window.addEventListener("resize", scheduleResize);
  window.addEventListener("orientationchange", scheduleResize);

  return { connect, disconnect, getCanvas, pushSize, setPointerShape };
}
