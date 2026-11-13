/* Records: uint32 LE sample position + 960 stereo s16le frames at 48 kHz.
 * Silent blocks are omitted; TCP/WebSocket boundaries do not delimit records. */
export class SessionAudio {
  constructor(base, { enabled = true } = {}) {
    this.url = `${base}/audio?token=audio`;
    this.sources = new Set();
    this.tail = new Uint8Array();
    this.stopped = !enabled;
    if (!enabled) return;
    this.onGesture = (event) => {
      if (!event?.isTrusted || this.stopped || document.hidden) return;
      // Safari needs the context AND a source started in the gesture itself.
      // A tap before the first network packet must not be wasted.
      this.ensureContext();
      this.resume();
      if (this.context && (!this.unlocked || this.context.state !== 'running')) {
        const source = this.context.createBufferSource();
        source.buffer = this.context.createBuffer(1, 1, this.context.sampleRate);
        source.connect(this.context.destination);
        source.onended = () => source.disconnect();
        source.start(0); // one silent local sample, no remote audio traffic
      }
    };
    this.onVisibility = () => {
      if (document.hidden) this.disconnect();
      else { this.resume(); this.start(); }
    };
    for (const type of ['pointerdown', 'pointerup', 'keydown', 'touchend', 'click']) {
      document.addEventListener(type, this.onGesture, true);
    }
    document.addEventListener('visibilitychange', this.onVisibility);
  }

  start() {
    if (this.stopped || document.hidden || this.socket) return;
    clearTimeout(this.retry);
    const socket = new WebSocket(this.url, ['binary']);
    this.socket = socket;
    socket.binaryType = 'arraybuffer';
    socket.onmessage = (event) => {
      if (this.socket === socket) this.receive(event.data);
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.clear();
      if (!this.stopped && !document.hidden) {
        this.retry = setTimeout(() => this.start(), 2000);
      }
    };
  }

  resume() {
    if (!this.context || this.stopped || document.hidden ||
        this.context.state === 'running' || this.context.state === 'closed') return;
    // Autoplay may leave this promise pending until a trusted gesture.
    const context = this.context;
    context.resume().then(() => {
      if (!this.stopped && this.context === context && context.state === 'running') {
        this.unlocked = true;
        this.flushPending();
      }
    }).catch(() => {});
  }

  ensureContext() {
    if (this.context || this.stopped || document.hidden) return;
    try {
      // Safari can otherwise classify Web Audio as ambient audio. Use its
      // supported media-playback category; other browsers need no special API.
      if (navigator.audioSession) {
        this.previousSessionType = navigator.audioSession.type;
        try { navigator.audioSession.type = 'playback'; } catch (_) {}
      }
      const Context = window.AudioContext || window.webkitAudioContext;
      const context = new Context({ latencyHint: 'interactive' });
      this.context = context;
      context.onstatechange = () => {
        if (this.stopped || this.context !== context) return;
        if (context.state === 'running') {
          this.unlocked = true;
          this.flushPending();
        } else {
          // Safari's interrupted state freezes the clock; discard scheduled
          // sources so foreground recovery cannot replay stale music.
          this.clear(false);
        }
      };
    } catch (_) { /* Unsupported playback stays silent. */ }
  }

  receive(data) {
    if (this.stopped || document.hidden) return;
    const bytes = new Uint8Array(this.tail.length + data.byteLength);
    bytes.set(this.tail);
    bytes.set(new Uint8Array(data), this.tail.length);
    const length = bytes.length - bytes.length % 3844;
    this.tail = bytes.slice(length);
    for (let offset = 0; offset < length; offset += 3844) {
      if (!this.context) { this.ensureContext(); this.resume(); }
      if (!this.context) return;
      const block = bytes.slice(offset, offset + 3844);
      if (this.context.state !== 'running') {
        // Keep at most the latest 250 ms while autoplay is blocked.
        this.pending ||= [];
        this.pending.push({ block, at: performance.now() });
        this.pending = this.pending.filter(item => performance.now() - item.at < 250).slice(-12);
      } else {
        this.flushPending();
        this.play(block);
      }
    }
  }

  flushPending() {
    const pending = this.pending || [];
    this.pending = [];
    if (this.stopped || document.hidden || this.context?.state !== 'running') return;
    for (const item of pending) {
      if (performance.now() - item.at < 250) this.play(item.block);
    }
  }

  play(block) {
    const view = new DataView(block.buffer, block.byteOffset, block.byteLength);
    const position = view.getUint32(0, true);
    const now = this.context.currentTime;
    let when = this.anchorTime + (position - this.anchorPosition) / 48000;
    if (!Number.isFinite(when) || when < now || when > now + 0.25) {
      this.clear(false);
      this.anchorTime = now + 0.04;
      this.anchorPosition = position;
      when = this.anchorTime;
    }
    const buffer = this.context.createBuffer(2, 960, 48000);
    for (let channel = 0; channel < 2; channel++) {
      const samples = buffer.getChannelData(channel);
      for (let i = 0; i < 960; i++) {
        samples[i] = view.getInt16(4 + i * 4 + channel * 2, true) / 32768;
      }
    }
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    source.onended = () => { this.sources.delete(source); source.disconnect(); };
    this.sources.add(source);
    source.start(when);
  }

  clear(resetStream = true) {
    for (const source of this.sources) { source.stop(); source.disconnect(); }
    this.sources.clear();
    this.anchorTime = undefined;
    this.anchorPosition = undefined;
    if (resetStream) { this.tail = new Uint8Array(); this.pending = []; }
  }

  disconnect() {
    clearTimeout(this.retry);
    const socket = this.socket;
    this.socket = null;
    if (socket) socket.close();
    this.clear();
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.disconnect();
    if (this.context) this.context.close().catch(() => {});
    for (const type of ['pointerdown', 'pointerup', 'keydown', 'touchend', 'click']) {
      document.removeEventListener(type, this.onGesture, true);
    }
    document.removeEventListener('visibilitychange', this.onVisibility);
    if (this.previousSessionType !== undefined && navigator.audioSession?.type === 'playback') {
      try { navigator.audioSession.type = this.previousSessionType; } catch (_) {}
    }
  }
}
