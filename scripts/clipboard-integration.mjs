// Integration: real session control channel and X11/Wine clipboard, with a
// deterministic simulated client clipboard. Native permission checks are covered
// separately by test-clipboard.mjs and clipboard-browser.mjs.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
const name=process.env.SESSION_CONTAINER || 'portos-auto-clipboard-check';
const url=process.env.CLIPBOARD_WS || 'ws://127.0.0.1:6089/ctl?token=ctl';
const source=await readFile(new URL('../web/viewer/clipboard.js',import.meta.url),'utf8');
const {SessionClipboard}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
let client='本机自动同步😀\n第二行';
globalThis.location={protocol:'https:'};
globalThis.window={isSecureContext:true,addEventListener(){},removeEventListener(){}};
globalThis.document={hidden:false,hasFocus:()=>true,addEventListener(){},removeEventListener(){}};
Object.defineProperty(globalThis,'navigator',{value:{clipboard:{async readText(){return client},async writeText(text){client=text}},permissions:{async query(){return {state:'granted'}}}},configurable:true});
const ws=new WebSocket(url,['binary']);ws.binaryType='arraybuffer';
await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
const encoder=new TextEncoder(),decoder=new TextDecoder();let buffer='';
const hints=[];
const sync=new SessionClipboard(msg=>{ws.send(encoder.encode(JSON.stringify(msg)+'\n'));return true},msg=>hints.push(msg));
ws.onmessage=({data})=>{buffer+=decoder.decode(data,{stream:true});let idx;while((idx=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,idx);buffer=buffer.slice(idx+1);sync.receive(JSON.parse(line));}};
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(test){for(let i=0;i<40;i++){if(await test())return;await pause(250)}throw Error('synchronization timeout: '+hints.join(';'));}
const get=()=>execFileSync('docker',['exec',name,'python3','-c','from clipboard_sync import read_clipboard; print(read_clipboard(),end="")'],{encoding:'utf8'});
try {
 sync.connect();await until(()=>sync.initialized&&!sync.pending);assert.equal(get(),client);
 const remote='Wine远端自动返回😀\n文本';
 execFileSync('docker',['exec',name,'python3','-c','import sys; from clipboard_sync import write_clipboard; assert write_clipboard(sys.argv[1])',remote]);
 await until(()=>client===remote);
 client='本机再次修改';await until(()=>get()===client);
 assert.ok(!hints.some(x=>x.includes('不可用')));
 console.log('PASS: automatic upload, real remote clipboard → client, second upload, protocol acknowledgement and no echo');
}finally{sync.stop();ws.close();}
