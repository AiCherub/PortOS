export function createToolbar({ isTouch, send, input, onEnd }) {
  const endBtn = document.getElementById("end-session");
  const confirmEl = document.getElementById("confirm");
  const confirmOk = document.getElementById("confirm-ok");
  const confirmCancel = document.getElementById("confirm-cancel");

  // The window list lets visitors restore windows minimized inside the session.
  const windowSwitcherEl = document.getElementById("window-switcher");
  const windowToggleEl = document.getElementById("window-toggle");
  const windowCountEl = document.getElementById("window-count");
  const windowListEl = document.getElementById("window-list");
  const windowMenuEl = document.getElementById("window-menu");
  const portrait = window.matchMedia("(orientation: portrait)");

  // What the bar is showing right now, so that it is only rebuilt when
  // something actually changed.
  let windowKey = "";
  let windowTimer = null;

  function updateWindowLayout() {
    const collapsed = portrait.matches && windowListEl.childElementCount > 0 &&
                      windowListEl.scrollWidth > windowListEl.clientWidth;
    if (!collapsed && !windowMenuEl.hidden) setWindowMenuOpen(false);
    windowToggleEl.hidden = !collapsed;
  }

  function renderWindowMenu() {
    const active = document.activeElement;
    const focusedId = windowMenuEl.contains(active) ? active.dataset.windowId : null;
    const buttons = Array.from(windowListEl.children, (button) => button.cloneNode(true));
    windowMenuEl.replaceChildren(...buttons);
    if (focusedId) {
      const focused = buttons.find((button) => button.dataset.windowId === focusedId);
      (focused || windowToggleEl).focus({ preventScroll: true });
    }
  }

  function setWindowMenuOpen(open) {
    if (!open && windowMenuEl.contains(document.activeElement)) {
      windowToggleEl.focus({ preventScroll: true });
    }
    windowMenuEl.hidden = !open;
    windowToggleEl.setAttribute("aria-expanded", String(open));
    if (open) renderWindowMenu();
  }

  new ResizeObserver(updateWindowLayout).observe(windowSwitcherEl);
  portrait.addEventListener("change", updateWindowLayout);
  document.fonts.ready.then(updateWindowLayout);

  windowToggleEl.addEventListener("click", () => setWindowMenuOpen(windowMenuEl.hidden));
  windowSwitcherEl.addEventListener("mousedown", (event) => {
    // Leave focus in the IME field when a window control is clicked.
    if (event.target.closest("button")) event.preventDefault();
  });
  windowSwitcherEl.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-window-id]");
    if (!button) return;
    send({ t: "activate", id: button.dataset.windowId });
    if (!windowMenuEl.hidden) setWindowMenuOpen(false);
    setTimeout(requestWindows, 300);
  });
  document.addEventListener("pointerdown", (event) => {
    if (!windowMenuEl.hidden && !windowSwitcherEl.contains(event.target)) {
      setWindowMenuOpen(false);
    }
  });

  function renderWindows(windows) {
    const key = JSON.stringify(windows);
    if (key === windowKey) return;
    windowKey = key;

    windowListEl.textContent = "";
    for (const win of windows) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.windowId = win.id;
      if (win.hidden) button.classList.add("minimized");
      if (win.focused) button.classList.add("focused");

      if (win.icon) {
        const icon = document.createElement("img");
        icon.className = "window-icon";
        icon.alt = "";
        icon.draggable = false;
        icon.src = `data:image/png;base64,${win.icon}`;
        button.appendChild(icon);
      }

      // The title comes from the program in the session. It is not text we
      // can trust, so it goes in as text and never as markup.
      const label = win.title || "(无名窗口)";
      const title = document.createElement("span");
      title.className = "window-title";
      title.textContent = label;
      button.title = label;
      button.appendChild(title);
      windowListEl.appendChild(button);
    }
    windowCountEl.textContent = `${windows.length} 个窗口`;
    updateWindowLayout();
    if (!windowMenuEl.hidden) renderWindowMenu();
  }

  function requestWindows() {
    send({ t: "windows" });
  }

  function startPolling() {
    requestWindows();
    stopPolling();
    windowTimer = setInterval(requestWindows, 2000);
  }

  function stopPolling() {
    clearInterval(windowTimer);
    windowTimer = null;
  }

  endBtn.addEventListener("mousedown", (event) => {
    // The focus has to stay in the hidden field, or the input method on the
    // visitor's own machine stops talking to the page.
    event.preventDefault();
  });

  /*
   * Ending the session throws the environment away - there is no way back.
   * On a phone the button sits right under the thumb, so it takes a second
   * to say yes.
   */
  function askToEnd() {
    if (!windowMenuEl.hidden) setWindowMenuOpen(false);
    confirmEl.hidden = false;
    // The field lets go of the focus, so that the keys of the visitor do
    // not reach the program while the question is on the screen.
    input.blurIME();
  }

  function stopAsking() {
    confirmEl.hidden = true;
    if (!isTouch) input.focusIME();
  }

  endBtn.addEventListener("click", askToEnd);
  confirmCancel.addEventListener("click", stopAsking);
  confirmOk.addEventListener("click", onEnd);

  function dismiss() {
    if (!confirmEl.hidden) {
      stopAsking();
      return true;
    }
    if (!windowMenuEl.hidden) {
      setWindowMenuOpen(false);
      return true;
    }
    return false;
  }

  function showEnding() {
    confirmEl.hidden = true;
    endBtn.disabled = true;
    endBtn.textContent = "正在退出…";
  }

  return { renderWindows, startPolling, stopPolling, dismiss, showEnding };
}
