// Reference receiver for validated preview-v1 messages. Reset synchronously on
// inventory/status changes, unsubscribe, permission revocation or disconnect.
const binding = ['version', 'subscriptionId', 'nodeId', 'sessionId', 'layoutRevision', 'monitorId'];

export class PreviewReceiver {
  constructor({paint, clear, ack}) {
    Object.assign(this, {paint, clear, ack});
    this.reset();
  }

  reset() {
    this.subscription = null;
    this.ready = false;
    this.displayed = 0;
    this.verifiedAt = -Infinity;
    this.latest = 0;
    this.clear();
  }

  subscribe(message, status) {
    this.reset();
    const session = status.session;
    if (message.kind !== 'preview.subscribe' || message.version !== 1 ||
        status.availability !== 'online' || status.nodeId !== message.nodeId ||
        session?.sessionId !== message.sessionId || session.layoutRevision !== message.layoutRevision ||
        !session.monitors.some(m => m.monitorId === message.monitorId)) return false;
    this.subscription = {...message};
    return true;
  }

  matches(message) {
    return this.subscription && binding.every(k => this.subscription[k] === message[k]);
  }

  accept(message) {
    if (!this.matches(message)) return false;
    if (message.kind === 'preview.ready') {
      this.ready = true;
      return true;
    }
    if (!this.ready || message.kind !== 'preview.verified' || message.seq !== this.displayed ||
        !this.displayed || Date.parse(message.verifiedAt) <= this.verifiedAt) return false;
    this.verifiedAt = Date.parse(message.verifiedAt);
    return true;
  }

  async frame(message, bytes, decode) {
    if (!this.ready || !this.matches(message) || message.kind !== 'preview.frame' ||
        message.seq <= this.latest || bytes.byteLength !== message.byteLength) return false;
    const generation = this.subscription;
    this.latest = message.seq;
    const image = await decode(bytes);
    try {
      if (generation !== this.subscription || message.seq !== this.latest) return false;
      this.paint(image);
      this.displayed = message.seq;
      this.verifiedAt = Date.parse(message.capturedAt);
      this.ack({...this.subscription, kind:'preview.ack', seq:message.seq});
      return true;
    } finally {
      image.close?.();
    }
  }
}
