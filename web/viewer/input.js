/*
 * Chinese is composed by the input method on the visitor's own machine - the
 * one in Windows, macOS or on their phone. The browser does the composing;
 * the program on the other side of the VNC connection is a Windows program
 * that only knows about key presses. So the composed text takes a detour:
 *
 *   input method -> the hidden field below -> the control channel
 *     -> the session container -> typed into the remote window
 *
 * Two things have to be arranged for that to work:
 *
 *   * the hidden field has to hold the focus, because an input method only
 *     talks to the focused editable field - which is why noVNC must not
 *     take the focus for its canvas;
 *   * the keys that go into the composition must not reach the remote. The
 *     pinyin a visitor types would otherwise show up in the remote program
 *     next to the Chinese they meant to type.
 */

export function createInput({ screen, getCanvas, isTouch, send, dismissToolbar }) {
  const imeEl = document.getElementById("ime");
  const softKeysBtn = document.getElementById("soft-keyboard");

  // The keys we sent down to the remote and have not sent up again.
  const sentDown = new Set();

  // Is the input method in the middle of composing a word?
  let composing = false;
  // What the field held when the composition started, so that a composition
  // that ends without data can still be recovered.
  let composeBase = "";
  // When the last composition ended. The key that confirms a candidate is
  // swallowed by the input method in Chrome and Firefox, but some browsers
  // deliver it right after the composition - and it must not overtake the
  // text on its way to the program.
  let composedAt = 0;
  // Characters that went to the program as key presses. A character that
  // turns up in the field without a key of its own was not sent yet - that
  // happens on soft keyboards, where there are no key events at all.
  let forwardedChars = 0;
  // Text that was composed before the control channel was up.
  let pendingText = [];

  /* An event the input method is dealing with rather than the program. */
  function isIMEEvent(event) {
    return event.isComposing ||
           event.keyCode === 229 ||
           event.key === "Process" ||
           event.key === "Unidentified";
  }

  /*
   * Hand a key to noVNC. The event is rebuilt and dispatched on the canvas, so
   * that noVNC's own keyboard code turns it into a keysym - exactly the path a
   * key press on the canvas would take.
   */
  function forwardKey(type, source) {
    const canvas = getCanvas();
    if (!canvas) return false;
    const event = new KeyboardEvent(type, {
      key: source.key,
      code: source.code,
      location: source.location,
      repeat: source.repeat,
      ctrlKey: source.ctrlKey,
      shiftKey: source.shiftKey,
      altKey: source.altKey,
      metaKey: source.metaKey,
      bubbles: true,
      cancelable: true,
    });
    // Not every browser takes keyCode from the constructor's options.
    if (source.keyCode && !event.keyCode) {
      try {
        Object.defineProperty(event, "keyCode", { get: () => source.keyCode });
      } catch (_) { /* ignore */ }
    }
    canvas.dispatchEvent(event);
    return true;
  }

  /*
   * Let go of every key we pressed. Without this the remote program would be
   * left with a stuck Ctrl or Shift when the field loses the focus.
   */
  function releaseKeys() {
    const canvas = getCanvas();
    if (!canvas) return;
    for (const code of sentDown) {
      canvas.dispatchEvent(new KeyboardEvent("keyup", {
        key: "", code: code, bubbles: true, cancelable: true,
      }));
    }
    sentDown.clear();
  }

  function focusIME() {
    try {
      imeEl.focus({ preventScroll: true });
    } catch (_) {
      imeEl.focus();
    }
  }

  /* Keep the field under the mouse, so the candidate window of the input
     method shows up next to the pointer. */
  function moveIME(x, y) {
    const left = Math.max(0, Math.min(x, window.innerWidth - 24));
    const top = Math.max(0, Math.min(y, window.innerHeight - 24));
    imeEl.style.left = left + "px";
    imeEl.style.top = top + "px";
  }

  function sendText(text) {
    if (!text) return;
    if (!send({ t: "text", text })) {
      // The control channel is not up yet; try again as soon as it is.
      pendingText.push(text);
    }
  }

  function flushPendingText() {
    for (const text of pendingText) send({ t: "text", text });
    pendingText = [];
  }

  function blurIME() {
    imeEl.blur();
  }

  /*
   * The keys that belong to the input method must not reach the remote
   * program. They are stopped before noVNC gets to see them - but without
   * preventDefault, so the input method can still do its composing.
   */
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && dismissToolbar()) {
      event.stopPropagation();
      event.preventDefault();
      return;
    }
    if (isIMEEvent(event)) event.stopPropagation();
  }, true);
  document.addEventListener("keyup", (event) => {
    if (isIMEEvent(event)) event.stopPropagation();
  }, true);

  /* Everything the visitor types goes to the remote program, not into the
     hidden field - but it reaches the remote through noVNC, so that the keys
     are turned into keysyms the way they always were. */
  imeEl.addEventListener("keydown", (event) => {
    if (isIMEEvent(event)) return;
    // The key that confirms a candidate belongs to the input method. Some
    // browsers deliver it right after the composition instead of swallowing
    // it, and it must not overtake the text on its way to the program.
    if (event.key === "Enter" && performance.now() - composedAt < 100) {
      return;
    }
    if (!forwardKey("keydown", event)) return;
    sentDown.add(event.code);
    if (event.key.length === 1) forwardedChars += 1;
  });

  imeEl.addEventListener("keyup", (event) => {
    if (isIMEEvent(event)) {
      // The input method swallowed the key, so it was never pressed in the
      // program either.
      sentDown.delete(event.code);
      return;
    }
    forwardKey("keyup", event);
    sentDown.delete(event.code);
  });

  imeEl.addEventListener("focusout", () => releaseKeys());

  imeEl.addEventListener("compositionstart", () => {
    composing = true;
    composeBase = imeEl.value;
  });

  imeEl.addEventListener("compositionend", (event) => {
    composing = false;
    composedAt = performance.now();
    let text = event.data || "";
    // Some browsers end a composition without saying what it was. Fall back
    // to what the field gained - but only if it really grew, or a cancelled
    // composition would send the old text a second time.
    if (!text && imeEl.value.length > composeBase.length &&
        imeEl.value.indexOf(composeBase) === 0) {
      text = imeEl.value.slice(composeBase.length);
    }
    composeBase = "";
    imeEl.value = "";
    sendText(text);
  });

  /*
   * Ordinary typing lands in this field as well - the browser does not know
   * that those keys are on their way to the remote program. Throw them away
   * again, so the field stays empty and the input method does not try to make
   * sense of text that was never meant for it.
   *
   * A soft keyboard is different: it produces no key events at all, so the
   * text that shows up here has not been sent to the program yet. That is
   * what the counter is for.
   */
  imeEl.addEventListener("input", (event) => {
    if (composing) return;
    if (event.inputType === "insertCompositionText") return;
    const value = imeEl.value;
    imeEl.value = "";
    if (event.inputType?.startsWith("insertFromPaste")) return;
    if (forwardedChars > 0) {
      // This character went to the program as a key press already.
      forwardedChars -= 1;
      return;
    }
    if (performance.now() - composedAt < 100) {
      // The text of the composition that just ended - it has been sent.
      return;
    }
    // A soft keyboard: it produces no key events at all, so this text has
    // not been sent to the program yet. A line break is left out: it belongs
    // to the return key, which went on as a key press.
    if (value && !/[\r\n]/.test(value)) sendText(value);
  });

  /* Clicking the picture must not take the focus away from the field. */
  screen.addEventListener("mousedown", () => {
    // On a touch device the keyboard would jump up over the picture, so
    // there it waits for the button.
    if (isTouch) return;
    setTimeout(focusIME, 0);
  }, true);

  /* The field follows the mouse, but not on every single mouse event. */
  let movePending = false;
  screen.addEventListener("mousemove", (event) => {
    if (composing || movePending) return;
    movePending = true;
    requestAnimationFrame(() => {
      movePending = false;
      moveIME(event.clientX, event.clientY);
    });
  }, true);
  window.addEventListener("focus", () => {
    if (!isTouch) focusIME();
  });
  /* Leaving the page releases the keys as well, so that the program is not
     left with a modifier held down. */
  window.addEventListener("blur", () => releaseKeys());

  moveIME(window.innerWidth / 2, window.innerHeight / 2);
  if (!isTouch) focusIME();

  /*
   * The soft keyboard.
   *
   * The device brings up its own keyboard when a text field gets the focus.
   * The field that carries the text is the one above - the very same one
   * the input method composes in - so whatever is typed on the phone's
   * keyboard ends up in the program the same way.
   */
  softKeysBtn.addEventListener("mousedown", (event) => {
    // The focus has to stay in the hidden field below, or the input method
    // on the visitor's own machine stops talking to the page.
    event.preventDefault();
  });

  softKeysBtn.addEventListener("click", () => {
    if (document.activeElement === imeEl) {
      // The keyboard is up already - put it away again.
      imeEl.blur();
      return;
    }
    // Put the field in the middle of the screen, so that the keyboard comes
    // up there and not in a corner.
    moveIME(window.innerWidth / 2, window.innerHeight / 2);
    focusIME();
  });

  return { focusIME, blurIME, flushPendingText };
}
