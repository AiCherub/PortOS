// Real browser + Wine regression. Requires compiled scripts/audio-tone.exe,
// an isolated running session, and Edge/Chrome with remote debugging enabled.
// SESSION_CONTAINER=... PORTOS_URL=http://127.0.0.1:6089/viewer.html
// CDP=http://127.0.0.1:9223 node scripts/audio-browser.mjs
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
let container = process.env.SESSION_CONTAINER || 'portos-audio-check';
const root = process.env.PORTOS_URL || 'http://127.0.0.1:6089/viewer.html';
const debug = process.env.CDP || 'http://127.0.0.1:9223';
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const tab = await (await fetch(`${debug}/json/new?about:blank`, { method: 'PUT' })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let id = 0;
const pending = new Map();
ws.onmessage = ({ data }) => {
  const msg = JSON.parse(data);
  if (msg.method === 'Runtime.exceptionThrown') console.error(JSON.stringify(msg.params.exceptionDetails));
  if (pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
};
async function call(method, params = {}) {
  const requestId = ++id;
  const response = new Promise(resolve => pending.set(requestId, resolve));
  ws.send(JSON.stringify({ id: requestId, method, params }));
  let timer;
  let result;
  try {
    result = await Promise.race([response, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error(`Timeout: ${method}`)), 20000);
    })]);
  } finally { clearTimeout(timer); }
  if (result.error) throw Error(JSON.stringify(result.error));
  return result.result;
}
async function evaluate(expression) {
  const result = await call('Runtime.evaluate', { expression, returnByValue: true });
  if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function click() {
  const [x, y] = [20, 20];
  for (const type of ['mousePressed', 'mouseReleased']) {
    await call('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 });
  }
}
try {
  await call('Page.enable');
  await call('Runtime.enable');
  await call('Page.addScriptToEvaluateOnNewDocument', { source: `
    window.audioProbe = { peak: 0, contexts: [], bytes: 0 };
    const NativeSocket = window.WebSocket;
    window.WebSocket = new Proxy(NativeSocket, {
      construct(target, args) {
        const socket = new target(...args);
        if (String(args[0]).includes('/audio?')) socket.addEventListener('message', e => audioProbe.bytes += e.data.byteLength || 0);
        return socket;
      }
    });
    const Original = window.AudioContext;
    window.AudioContext = class extends Original {
      constructor(...args) {
        super(...args); audioProbe.contexts.push(this);
        const analyser = this.createAnalyser();
        const original = this.createBufferSource.bind(this);
        this.createBufferSource = () => { const node=original(); node.connect(analyser); return node; };
        const values = new Float32Array(analyser.fftSize);
        setInterval(() => { analyser.getFloatTimeDomainData(values);
          for (const n of values) audioProbe.peak=Math.max(audioProbe.peak,Math.abs(n)); }, 10);
      }
    };` });
  for (let i = 0; i < 40; i++) {
    try { if ((await fetch(root)).ok) break; } catch (_) {}
    await pause(500);
  }
  await call('Page.navigate', { url: root });
  for (let i = 0; i < 60; i++) {
    if (await evaluate(`!!document.querySelector('#overlay.gone')`)) break;
    await pause(500);
  }
  assert.ok(await evaluate(`!!document.querySelector('#overlay.gone')`), 'VNC connected');
  if (!process.env.SESSION_CONTAINER) {
    const sid = await evaluate(`(location.pathname.match(/^\\/s\\/([0-9a-f]{32})/) || [])[1]`);
    if (sid) container = `portos-${sid}`;
  }
  await pause(1000);
  if (process.env.AUDIO_DEMO === '1') {
    await call('Emulation.setTouchEmulationEnabled', { enabled: true });
    await call('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: 20, y: 20 }] });
    await call('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    // The current Mario fixture waits on its focused Play button. Start it
    // after the browser touch to exercise gesture-before-first-audio ordering.
    const windowId = execFileSync('docker', ['exec', container, 'xdotool', 'search', '--name', '马里奥'], { encoding: 'utf8' }).trim().split(/\s+/).at(-1);
    execFileSync('docker', ['exec', container, 'xdotool', 'windowactivate', '--sync', windowId]);
    execFileSync('docker', ['exec', container, 'xdotool', 'key', '--clearmodifiers', 'space']);
    await pause(2500);
    const result = await evaluate(`({bytes:audioProbe.bytes,peak:audioProbe.peak,state:audioProbe.contexts[0]?.state})`);
    assert.ok(result.bytes > 0, 'demo emits audio');
    assert.equal(result.state, 'running');
    assert.ok(result.peak > 0.01, 'demo audio reaches running browser graph');
    console.log('PASS: current Wine demo → browser audio after touch:', result);
  } else {
  assert.equal(await evaluate(`!!document.querySelector('#toggle-audio')`), false);
  assert.equal(await evaluate('audioProbe.contexts.length'), 0, 'silence does not request playback');
  assert.equal(await evaluate('audioProbe.bytes'), 0, 'silence sends no audio payload');
  execFileSync('docker', ['cp', 'scripts/audio-tone.exe', `${container}:/opt/apps/audio-tone.exe`]);
  execFileSync('docker', ['exec', container, 'python3', '-c',
    'import wave,math,struct; w=wave.open("/tmp/tone.wav","wb"); w.setparams((2,2,48000,0,"NONE","not compressed")); w.writeframes(b"".join(struct.pack("<hh",int(12000*math.sin(2*math.pi*440*i/48000)),int(12000*math.sin(2*math.pi*660*i/48000))) for i in range(48000*3))); w.close()']);
  execFileSync('docker', ['exec', '-e', 'WINEPREFIX=/opt/wineprefix-template', '-e', 'PULSE_SERVER=unix:/tmp/runtime/pulse/native', container,
    'wine', '/opt/apps/audio-tone.exe', 'Z:\\tmp\\tone.wav'], { timeout: 30000 });
  await pause(500);
  assert.equal(await evaluate('audioProbe.contexts.length'), 1, 'first sound creates playback context');
  if (process.env.EXPECT_AUTOPLAY === '1') {
    assert.ok(await evaluate('audioProbe.peak > 0.1'), 'allowed autoplay starts without any gesture');
  }
  // A normal interaction with the program silently unlocks restricted browsers.
  await click();
  execFileSync('docker', ['exec', '-e', 'WINEPREFIX=/opt/wineprefix-template', '-e', 'PULSE_SERVER=unix:/tmp/runtime/pulse/native', container,
    'wine', '/opt/apps/audio-tone.exe', 'Z:\\tmp\\tone.wav'], { timeout: 30000 });
  await pause(500);
  const peak = await evaluate('audioProbe.peak');
  assert.ok(peak > 0.1, `Wine output reached browser audio graph: ${peak}`);
  await pause(1000);
  const quietBytes = await evaluate('audioProbe.bytes');
  await pause(1500);
  assert.equal(await evaluate('audioProbe.bytes'), quietBytes, 'transmission stops when program becomes silent');
  assert.equal(await evaluate('audioProbe.contexts[0].state'), 'running');
  console.log(`PASS: no button, no silent payload/context, lazy activation, Wine audio peak=${peak}, silent stop`);
  }
} finally {
  await call('Page.close').catch(() => {});
  ws.close();
}
