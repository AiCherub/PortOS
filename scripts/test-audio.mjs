import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const code = await readFile(new URL('../web/viewer/audio.js', import.meta.url), 'utf8');
const { SessionAudio } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
const player = new SessionAudio('ws://localhost');
player.onGesture();
assert.equal(player.context, undefined, 'no audio context before first sound');
const buffers = [], sources = [];
player.context = {
  state: 'running', currentTime: 1, destination: {},
  createBuffer(channels, frames, rate) {
    const data = Array.from({ length: channels }, () => new Float32Array(frames));
    buffers.push(data);
    return { getChannelData: c => data[c], duration: frames / rate };
  },
  createBufferSource() {
    const source = { connect() {}, disconnect() {}, start(when) { this.when = when; }, stop() { this.stopped = true; } };
    sources.push(source);
    return source;
  },
};
function record(position) {
  const bytes = new Uint8Array(3844);
  new DataView(bytes.buffer).setUint32(0, position, true);
  bytes.set([0, 128, 255, 127, 0, 64, 0, 192], 4);
  return bytes;
}
const bytes = record(0);
for (const chunk of [bytes.slice(0, 1), bytes.slice(1, 5), bytes.slice(5)]) player.receive(chunk.buffer);
assert.equal(buffers.length, 1);
assert.equal(buffers[0][0][0], -1);
assert.equal(buffers[0][1][0], 32767 / 32768);
assert.equal(buffers[0][0][1], 0.5);
assert.equal(buffers[0][1][1], -0.5);
player.receive(record(4800).buffer);
assert.ok(Math.abs(sources[1].when - sources[0].when - 0.1) < 0.00001, 'omitted silence preserves timing');
for (let i = 0; i < 100; i++) player.receive(record(i * 960).buffer);
assert.ok(player.sources.size <= 13);
assert.ok(sources.at(-1).when <= 1.25);
player.context.state = 'suspended';
for (let i = 0; i < 100; i++) player.receive(record(i * 960).buffer);
assert.ok(player.pending.length <= 12, 'blocked autoplay buffer bounded');
const active = [...player.sources];
player.disconnect();
assert.ok(active.every(source => source.stopped));
assert.equal(player.sources.size, 0);
assert.equal(player.tail.length, 0);
assert.equal(player.pending.length, 0);
console.log('PASS: lazy activation, fragmentation, stereo, silence timing, bounded buffering, cleanup');

// Safari model: a trusted tap before any PCM arrives must create and prime the
// context synchronously; returning from interrupted must resume the same one.
const sessions = [];
Object.defineProperty(globalThis, 'navigator', {value: {audioSession: {type: 'auto'}}, configurable: true});
globalThis.window = {AudioContext: class {
  constructor() { this.state='suspended'; this.currentTime=1; this.sampleRate=44100; this.destination={}; this.started=[]; this.resumes=0; sessions.push(this); }
  resume() { this.resumes++; this.state='running'; return Promise.resolve(); }
  close() { this.state='closed'; return Promise.resolve(); }
  createBuffer(channels,frames,rate) { return {getChannelData:()=>new Float32Array(frames),duration:frames/rate}; }
  createBufferSource() { const context=this; return {connect(){},disconnect(){},stop(){},start(at){context.started.push(at)}}; }
}};
const ios = new SessionAudio('ws://localhost');
assert.equal(ios.context,undefined);
ios.onGesture({isTrusted:false});
assert.equal(ios.context,undefined,'synthetic events do not unlock');
ios.onGesture({isTrusted:true});
assert.equal(sessions.length,1,'early gesture creates context before PCM');
assert.equal(sessions[0].resumes,1);
assert.equal(sessions[0].started.length,1,'silent source started synchronously in gesture');
assert.equal(navigator.audioSession.type,'playback');
await Promise.resolve();
ios.receive(record(0).buffer);
assert.equal(ios.sources.size,1);
ios.context.state='interrupted';ios.context.onstatechange();
assert.equal(ios.sources.size,0,'interruption drops stale scheduled audio');
let connections=0;ios.start=()=>connections++;
ios.onVisibility();await Promise.resolve();
assert.equal(ios.context.state,'running');assert.equal(ios.context.resumes,2);
assert.equal(connections,1);
ios.stop();
assert.equal(navigator.audioSession.type,'auto');
ios.onGesture({isTrusted:true});assert.equal(sessions.length,1,'ended session cannot reopen audio');
console.log('PASS: Safari early trusted gesture, synchronous silent prime, playback category, interruption recovery, cleanup');

const disabled = new SessionAudio('ws://localhost', { enabled: false });
const contextCount = sessions.length;
disabled.start();
disabled.ensureContext();
disabled.receive(record(0).buffer);
disabled.resume();
disabled.stop();
assert.equal(disabled.socket, undefined);
assert.equal(disabled.context, undefined);
assert.equal(disabled.onGesture, undefined, 'disabled audio installs no gesture listeners');
assert.equal(sessions.length, contextCount);
console.log('PASS: disabled audio creates no socket, context, or gesture listeners');
