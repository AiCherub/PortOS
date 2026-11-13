import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const code=await readFile(new URL('../web/viewer/clipboard.js',import.meta.url),'utf8');
const {SessionClipboard}=await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
let local,reads,writes,queries,focused,queryMode,readMode,readStatus,writeStatus;
const listeners=new Map();
globalThis.window={isSecureContext:true,addEventListener(){},removeEventListener(){}};
globalThis.location={protocol:'https:'};
globalThis.document={hidden:false,hasFocus:()=>focused,addEventListener(t,f){listeners.set(t,f)},removeEventListener(){},getElementById(){return {contains:()=>false}}};
Object.defineProperty(globalThis,'navigator',{value:{clipboard:{
  async readText(){reads++;if(readMode==='deny')throw Object.assign(Error(),{name:'NotAllowedError'});return local;},
  async writeText(text){writes.push(text);local=text;}
},permissions:{async query({name}){queries.push(name);if(queryMode==='unsupported')throw TypeError();return name==='clipboard-read'?readStatus:writeStatus;}}},configurable:true});
const settle=()=>new Promise(resolve=>setTimeout(resolve,10));
function make(mode){
  local='本机初始😀';reads=0;writes=[];queries=[];focused=true;queryMode='ok';readMode='ok';
  readStatus={state:'prompt'};writeStatus={state:'granted'};
  location.protocol='https:';window.isSecureContext=true;document.hidden=false;
  const sent=[],hints=[];
  const sync=new SessionClipboard(msg=>{sent.push(msg);return true},text=>hints.push(text),mode);
  return {sync,sent,hints};
}
function ack(sync,sent){sync.receive({t:'clipboard-set',id:sent.at(-1).id,ok:true});}
for (const mode of [undefined, 'both']) {
 const {sync,sent,hints}=make(mode);sync.connect();await settle();
 assert.equal(sync.ready,true);assert.ok(reads>0,'read invoked on entry to trigger native permission');
 assert.equal(sent[0].text,'本机初始😀');ack(sync,sent);
 local='本机更新\r\n多行';await sync.tick();assert.equal(sent.at(-1).text,'本机更新\n多行');
 sync.receive({t:'clipboard',text:'旧的远端消息'});assert.equal(local,'本机更新\r\n多行','pending upload cannot echo stale remote');
 ack(sync,sent);sync.receive({t:'clipboard',text:'会话更新😀'});await settle();
 assert.equal(local,'会话更新😀');const count=sent.length;await sync.tick();assert.equal(sent.length,count,'no feedback loop');
 focused=false;local='失焦时本机复制';await sync.tick();assert.equal(sent.length,count);
 sync.receive({t:'clipboard',text:'后台远端变化'});focused=true;await sync.tick();assert.equal(sent.at(-1).text,'失焦时本机复制','local change wins on focus');ack(sync,sent);
 sync.disconnect();sync.connect();await settle();assert.equal(sent.at(-1).text,local,'reconnect initial upload');
 const before=reads;sync.stop();await sync.tick();assert.equal(reads,before);
 assert.ok(!hints.some(x=>x.includes('不可用')));
}
for(const mode of ['http','unsupported','denied','api-denied']){
 const {sync,sent,hints}=make();
 if(mode==='http')location.protocol='http:';
 if(mode==='unsupported')queryMode='unsupported';
 if(mode==='denied')readStatus.state='denied';
 if(mode==='api-denied')readMode='deny';
 sync.connect();await settle();assert.equal(sync.disabled,true,mode);assert.equal(sent.length,0);
 assert.ok(hints[0].includes('不可用'));if(mode==='http')assert.equal(reads,0);
 let prevented=false;sync.onPaste({target:{id:'ime'},preventDefault(){prevented=true},stopImmediatePropagation(){},clipboardData:{types:['text/plain'],getData:()=> '不应传输'}});
 assert.ok(prevented);assert.equal(sent.length,0,'disabled paste never leaks through IME');sync.stop();
}
{
 const {sync,hints}=make();sync.connect();await settle();readStatus.state='denied';readStatus.onchange();
 assert.equal(sync.ready,false);assert.ok(hints.at(-1).includes('撤销'));sync.stop();
}

function paste(sync, text) {
  let prevented = false;
  sync.onPaste({
    target: { id: 'ime' },
    preventDefault() { prevented = true; },
    stopImmediatePropagation() {},
    clipboardData: { types: ['text/plain'], getData: () => text },
  });
  assert.ok(prevented, 'the hidden IME field must not receive browser paste');
}

function shortcut(sync, key) {
  let intercepted = false;
  sync.onKey({
    target: { id: 'ime' }, key, ctrlKey: true,
    preventDefault() {},
    stopImmediatePropagation() { intercepted = true; },
  });
  return intercepted;
}

{
  const {sync, sent, hints} = make('out');
  readStatus.state = 'denied';
  sync.connect();
  await settle();
  assert.deepEqual(queries, ['clipboard-write']);
  assert.equal(reads, 0);
  assert.deepEqual(writes, []);
  assert.deepEqual(sent, [{ t: 'clipboard-get' }]);
  sync.receive({ t: 'clipboard', text: '会话内容\r\n多行' });
  await settle();
  assert.deepEqual(writes, ['会话内容\n多行']);
  paste(sync, '禁止传入');
  assert.equal(sync.submit('禁止传入'), false);
  assert.equal(shortcut(sync, 'v'), false);
  assert.equal(shortcut(sync, 'x'), true);
  assert.deepEqual(sent.slice(1), [{ t: 'clipboard-copy', cut: true }]);
  sync.disconnect();
  sync.connect();
  await settle();
  assert.deepEqual(sent.at(-1), { t: 'clipboard-get' });
  assert.equal(reads, 0);
  assert.deepEqual(hints, []);
  sync.stop();
}

{
  const {sync, sent, hints} = make('in');
  writeStatus.state = 'denied';
  sync.connect();
  await settle();
  assert.deepEqual(queries, ['clipboard-read']);
  assert.equal(sent[0].text, local);
  ack(sync, sent);
  sync.receive({ t: 'clipboard', text: '禁止传出' });
  await sync.tick();
  assert.deepEqual(writes, []);
  assert.equal(shortcut(sync, 'c'), false);
  assert.equal(shortcut(sync, 'v'), true);
  paste(sync, '允许传入');
  assert.equal(sent.length, 2);
  assert.equal(sent.at(-1).text, '允许传入');
  assert.equal(sent.at(-1).paste, true);
  assert.deepEqual(hints, []);
  sync.stop();
}

{
  const {sync, sent, hints} = make('disabled');
  sync.connect();
  await settle();
  assert.equal(sync.ready, false);
  sync.receive({ t: 'clipboard', text: '禁止传出' });
  paste(sync, '禁止传入');
  assert.equal(shortcut(sync, 'c'), false);
  assert.equal(shortcut(sync, 'v'), false);
  assert.equal(reads, 0);
  for (const list of [writes, queries, sent, hints]) assert.deepEqual(list, []);
  sync.stop();
}

console.log('PASS: clipboard modes, directional permissions, shortcuts and paste, focus/reconnect, permission failures and echo suppression');
