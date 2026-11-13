const hintEl = document.getElementById("hint");
const overlayEl = document.getElementById("overlay");
const overlayStatusEl = document.getElementById("overlay-status");

export function hideOverlay() {
  overlayEl.classList.add("gone");
}

/* Replace the loading overlay with a message the visitor can act on. */
export function showOverlayMessage(title, subtitle, action) {
  const heading = document.createElement("p");
  heading.className = "overlay-title";
  heading.textContent = title;
  const detail = document.createElement("p");
  detail.className = "overlay-sub";
  detail.textContent = subtitle;
  overlayStatusEl.replaceChildren(heading, detail);
  if (action) {
    const link = document.createElement("a");
    link.className = "overlay-link";
    link.href = action.href;
    link.textContent = action.label;
    overlayStatusEl.append(link);
  }
  overlayEl.classList.remove("gone");
}

let hintTimer = null;
const hintQueue = [];
export function flashHint(text, duration) {
  if (!text) return;
  hintQueue.push({ text, duration: duration || 2500 });
  if (hintTimer === null) showNextHint();
}

function showNextHint() {
  const next = hintQueue.shift();
  if (!next) {
    hintEl.hidden = true;
    hintTimer = null;
    return;
  }
  hintEl.textContent = next.text;
  hintEl.hidden = false;
  hintTimer = setTimeout(showNextHint, next.duration);
}

export function clearHints() {
  clearTimeout(hintTimer);
  hintTimer = null;
  hintQueue.length = 0;
  hintEl.hidden = true;
}
