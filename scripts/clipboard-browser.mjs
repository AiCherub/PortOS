// Text clipboard regression. Requires an isolated Wine Notepad session
// and Edge/Chrome with remote debugging enabled; uses its clipboard.
// SESSION_CONTAINER=... PORTOS_URL=http://127.0.0.1:6089/viewer.html
// CDP=http://127.0.0.1:9223 node scripts/audio-browser.mjs
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
let container = process.env.SESSION_CONTAINER || 'portos-clipboard-check';
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
  for(let i=0;i<40;i++) {try{if((await fetch(root)).ok)break}catch(_){} await pause(500)}
  await call('Page.navigate',{url:root});
  for(let i=0;i<60;i++) {
    if(await evaluate(`!!document.querySelector('#overlay.gone') && !document.querySelector('#hint').hidden && document.querySelector('#hint').textContent.includes('HTTP')`))break;
    await pause(500);
  }
  assert.ok(await evaluate(`!!document.querySelector('#overlay.gone')`),'VNC still works');
  assert.equal(await evaluate(`document.querySelectorAll('#clipboard-toggle,#clipboard-panel').length`),0);
  const status=await evaluate(`document.querySelector('#hint').textContent`);
  assert.ok(status.includes('HTTP') && status.includes('HTTPS'),status);
  console.log('PASS: HTTP explains unsupported clipboard; no clipboard controls or dialog; VNC unaffected');
} finally {await call('Page.close').catch(()=>{});ws.close();}
