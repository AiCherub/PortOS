/* Automatic text clipboard sync. Native browser permission prompt only;
 * unsupported environments get a reason, never a manual clipboard UI. */
export class SessionClipboard {
  constructor(send, hint, mode = 'both') {
    this.send = send;
    this.hint = hint;
    this.allowIn = mode === 'both' || mode === 'in';
    this.allowOut = mode === 'both' || mode === 'out';
    this.active = true;
    this.connected = false;
    this.ready = false;
    this.sequence = 0;
    this.epoch = 0;
    this.onFocus = () => this.start();
    window.addEventListener('focus', this.onFocus);
    document.addEventListener('visibilitychange', this.onFocus);
    this.onKey = (event) => {
      if (!this.isRemote(event) || event.isComposing) return;
      const key = event.key.toLowerCase();
      const shortcut = (event.ctrlKey || event.metaKey) && !event.altKey;
      if (this.allowOut && shortcut && (key === 'c' || key === 'x')) {
        event.stopImmediatePropagation();
        event.preventDefault();
        this.send({ t: 'clipboard-copy', cut: key === 'x' });
      } else if (this.allowIn && ((shortcut && key === 'v') || (event.shiftKey && key === 'insert'))) {
        event.stopImmediatePropagation();
      }
    };
    this.onPaste = (event) => {
      if (!this.active || !(event.target.id === 'ime' || document.getElementById('screen').contains(event.target))) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!this.allowIn || !this.isRemote(event)) return;
      if (event.clipboardData?.types.includes('text/plain')) {
        this.submit(event.clipboardData.getData('text/plain'), true);
      } else this.hint('剪贴板仅支持文本，暂不支持图片和文件', 5000);
    };
    document.addEventListener('keydown', this.onKey, true);
    document.addEventListener('paste', this.onPaste, true);
  }

  available() {
    return this.active && this.connected && !this.disabled &&
      !document.hidden && document.hasFocus();
  }

  fail(reason) {
    this.disabled = true;
    this.ready = false;
    clearInterval(this.timer);
    this.hint(`自动剪贴板不可用：${reason}`, 12000);
  }

  connect() {
    this.epoch++;
    this.connected = true;
    this.initialized = false;
    this.pending = null;
    this.incoming = null;
    this.start();
  }

  disconnect() {
    this.epoch++;
    this.connected = false;
    this.initialized = false;
    this.pending = null;
    this.incoming = null;
    clearInterval(this.timer);
  }

  async start() {
    if ((!this.allowIn && !this.allowOut) || !this.active || !this.connected || this.disabled || this.starting) return;
    if (location.protocol !== 'https:' || !window.isSecureContext) {
      this.fail('当前使用 HTTP 或非安全连接，请通过 HTTPS 访问会话');
      return;
    }
    if ((this.allowIn && !navigator.clipboard?.readText) ||
        (this.allowOut && !navigator.clipboard?.writeText) || !navigator.permissions?.query) {
      this.fail('当前浏览器不支持所需的剪贴板操作或权限授权');
      return;
    }
    if (!this.available()) return; // Browser APIs require a foreground document.
    this.starting = true;
    const epoch = this.epoch;
    try {
      if (!this.permissions) {
        const names = [];
        if (this.allowIn) names.push('clipboard-read');
        if (this.allowOut) names.push('clipboard-write');
        let permissions;
        try {
          permissions = await Promise.all(names.map(name => navigator.permissions.query({ name })));
        } catch (_) {
          if (this.available() && epoch === this.epoch) this.fail('当前浏览器不支持持续剪贴板权限');
          return;
        }
        if (!this.available() || epoch !== this.epoch) return;
        this.permissions = permissions;
        for (const permission of this.permissions) {
          permission.onchange = () => {
            if (permission.state === 'denied' && this.active) this.fail('剪贴板权限已被拒绝或撤销，请在站点设置中允许后刷新');
          };
        }
      }
      if (this.permissions.some(permission => permission.state === 'denied')) {
        this.fail('剪贴板权限被拒绝，请在站点设置中允许后刷新');
        return;
      }
      // Query does not prompt. The actual read triggers the browser's native
      // permission request on entry, without inventing a website permission UI.
      if (!this.ready && this.allowIn) {
        const text = await navigator.clipboard.readText();
        if (!this.available() || epoch !== this.epoch) return;
        if (this.allowOut) {
          // Probe write permission without changing the clipboard content.
          await navigator.clipboard.writeText(text);
          if (!this.available() || epoch !== this.epoch) return;
        }
      }
      this.ready = true;
      clearInterval(this.timer);
      this.timer = setInterval(() => this.tick(), 500);
      await this.tick();
    } catch (error) {
      if (this.available() && epoch === this.epoch) this.apiFailure(error);
    } finally {
      this.starting = false;
      if (epoch !== this.epoch) this.start();
    }
  }

  apiFailure(error) {
    this.fail(error?.name === 'NotAllowedError'
      ? '浏览器拒绝访问剪贴板：权限未授予、需要额外交互，或被站点策略限制'
      : '浏览器无法访问系统剪贴板，请检查浏览器和系统权限');
  }

  isRemote(event) {
    return this.ready && this.available() &&
      (event.target.id === 'ime' || document.getElementById('screen').contains(event.target));
  }

  normalize(text) { return text.replace(/\r\n?/g, '\n'); }

  submit(text, paste = false) {
    if (!this.allowIn || !this.ready || !this.available()) return false;
    text = this.normalize(text);
    if (new TextEncoder().encode(text).length > 65536 || text.includes('\0')) {
      if (this.rejected !== text) this.hint('剪贴板仅支持不超过 64 KiB 的文本，当前内容未同步', 6000);
      this.rejected = text;
      return false;
    }
    this.rejected = undefined;
    const id = ++this.sequence;
    if (!this.send({ t: 'clipboard-set', text, paste, id })) return false;
    this.lastLocal = text;
    this.pending = { id, at: Date.now() };
    this.incoming = null;
    this.initialized = true;
    return true;
  }

  async tick() {
    if (!this.ready || !this.available() || this.busy) return;
    if (this.pending) {
      if (Date.now() - this.pending.at > 10000) this.fail('会话剪贴板同步超时，请重新连接');
      return;
    }
    this.busy = true;
    const epoch = this.epoch;
    const sequence = this.sequence;
    try {
      if (this.allowIn) {
        const text = this.normalize(await navigator.clipboard.readText());
        if (!this.available() || epoch !== this.epoch || sequence !== this.sequence || this.pending) return;
        // On entry and after reconnect, the local clipboard is authoritative.
        // Local changes also win when both sides changed while focus was away.
        if (!this.initialized || text !== this.lastLocal) {
          this.submit(text);
          return;
        }
      } else if (!this.initialized) {
        this.initialized = this.send({ t: 'clipboard-get' });
      }
      const incoming = this.incoming;
      if (!incoming) return;
      if (!this.allowIn || incoming.text !== this.lastLocal) {
        await navigator.clipboard.writeText(incoming.text);
        if (!this.available() || epoch !== this.epoch) return;
        this.lastLocal = incoming.text;
      }
      if (this.incoming === incoming) this.incoming = null;
    } catch (error) {
      if (this.available() && epoch === this.epoch) this.apiFailure(error);
    } finally { this.busy = false; }
  }

  receive(msg) {
    if (!this.active || !this.connected || !this.ready) return;
    if (msg.t === 'clipboard-set' && this.pending && this.pending.id === msg.id) {
      this.pending = null;
      if (!msg.ok) this.fail('会话端无法更新剪贴板，请重新连接');
      return;
    }
    if (!this.allowOut || msg.t !== 'clipboard' || this.pending || !this.initialized || typeof msg.text !== 'string') return;
    this.incoming = { text: this.normalize(msg.text) };
    this.tick();
  }

  stop() {
    this.active = false;
    this.disconnect();
    this.lastLocal = this.rejected = undefined;
    window.removeEventListener('focus', this.onFocus);
    document.removeEventListener('visibilitychange', this.onFocus);
    document.removeEventListener('keydown', this.onKey, true);
    document.removeEventListener('paste', this.onPaste, true);
    for (const permission of this.permissions || []) permission.onchange = null;
  }
}
