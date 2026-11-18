import { PreviewReceiver } from "/receiver.mjs";

import { bindInput } from "/input.js";
const el = (id) => document.getElementById(id);
let state, focus;
const cards = new Map();
const viewport = el("viewport");
let zoom = 1;
viewport.addEventListener(
  "wheel",
  (event) => {
    if (!event.ctrlKey || focus || !event.deltaY) return;
    event.preventDefault();
    const delta =
      event.deltaY *
      (event.deltaMode === 1
        ? 16
        : event.deltaMode === 2
          ? viewport.clientHeight
          : 1);
    const next = Math.min(
      4,
      Math.max(
        0.5,
        zoom * Math.exp(-Math.max(-120, Math.min(120, delta)) * 0.002),
      ),
    );
    const x = event.clientX - viewport.getBoundingClientRect().left;
    const y = event.clientY - viewport.getBoundingClientRect().top;
    const ratio = next / zoom;
    const left = (viewport.scrollLeft + x) * ratio - x,
      top = (viewport.scrollTop + y) * ratio - y;
    zoom = next;
    el("grid").style.setProperty("--scale", zoom);
    viewport.scrollTo(left, top);
  },
  { passive: false },
);
document.addEventListener("keydown", (event) => {
  if (!focus && event.ctrlKey && event.key === "0") {
    event.preventDefault();
    zoom = 1;
    el("grid").style.removeProperty("--scale");
    viewport.scrollTo(0, 0);
  }
});
const socket = (control = false) =>
  new WebSocket(
    "wss://" + location.host + "/stream",
    control ? "classctl.input.v1" : "classctl.preview.v1",
  );
function clearGrid() {
  for (const card of cards.values()) card.stop();
  cards.clear();
  el("grid").replaceChildren();
}
function closeFocus() {
  if (!focus) return;
  const current = focus;
  focus = null;
  setDrawer(false);
  if (document.fullscreenElement === el("focus"))
    document.exitFullscreen().catch(() => {});
  current.cleanup?.();
  current.ws.close();
  current.peer.close();
  el("focus").querySelector("video")?.remove();
  el("focus").hidden = true;
  el("grid").hidden = false;
}
function back() {
  closeFocus();
  if (state) renderGrid();
}
el("back").onclick = back;
function setDrawer(open) {
  el("tools").classList.toggle("open", open);
  el("handle").setAttribute("aria-expanded", String(open));
  el("handle").textContent = open ? "›" : "‹";
  el("drawer").inert = !open;
}
el("tools").onpointerenter = (e) => {
  if (e.pointerType === "mouse") setDrawer(true);
};
el("tools").onpointerleave = (e) => {
  if (
    e.pointerType === "mouse" &&
    !el("drawer").contains(document.activeElement)
  )
    setDrawer(false);
};
el("handle").onclick = (e) =>
  setDrawer(
    e.pointerType === "mouse" || !el("tools").classList.contains("open"),
  );
el("tools").onfocusout = (e) => {
  if (!el("tools").contains(e.relatedTarget)) setDrawer(false);
};
document.addEventListener("fullscreenchange", () => {
  if (!focus) return;
  if (document.fullscreenElement === el("focus")) focus.fullscreen = true;
  else if (focus.fullscreen) back();
});
document.addEventListener(
  "keydown",
  (e) => {
    if (focus && e.key === "Escape") {
      e.preventDefault();
      e.stopImmediatePropagation();
      back();
    }
  },
  true,
);
function signature(seat) {
  const node = state.nodes[seat.nodeId];
  return JSON.stringify([
    seat,
    node?.session,
    node?.availability,
    state.controlNodes?.includes(seat.nodeId),
  ]);
}
async function openView(seat, id) {
  const binding = state.nodes[seat.nodeId];
  const control = state.controlNodes?.includes(seat.nodeId) ?? false;
  const ws = socket(control),
    peer = new RTCPeerConnection({ iceServers: state.iceServers });
  const active = { ws, peer, seat, signature: signature(seat) };
  focus = active;
  clearGrid();
  el("grid").hidden = true;
  el("focus").hidden = false;
  // The browser requires user activation here, before any network await.
  el("focus")
    .requestFullscreen?.()
    .then(() => {
      active.fullscreen = true;
    })
    .catch(() => {});
  el("notice").textContent = "";
  el("target").textContent = seat.seatId + " · " + id;
  el("mode").textContent = "Подключение…";
  const request = {
    version: 1,
    kind: "access",
    nodeId: binding.nodeId,
    sessionId: binding.session.sessionId,
    layoutRevision: binding.session.layoutRevision,
    mode: control ? "control" : "view",
    target: { scope: "monitor", monitorId: id },
  };
  const video = document.createElement("video");
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  video.onplaying = () => {
    if (focus !== active || active.playing) return;
    active.playing = true;

    if (control) {
      active.cleanup = bindInput(video, ws);
      el("mode").textContent = "Управление включено";
    } else el("mode").textContent = "Режим просмотра";
  };
  el("focus").append(video);
  const stop = () => {
    if (focus !== active) return;
    back();
    el("notice").textContent = "Соединение с монитором завершено";
  };
  ws.onclose = stop;
  ws.onerror = stop;
  peer.ontrack = (e) => {
    video.srcObject = new MediaStream([e.track]);
  };
  peer.addTransceiver("video", { direction: "recvonly" });
  ws.onmessage = async (e) => {
    try {
      await peer.setRemoteDescription(JSON.parse(e.data));
    } catch {
      stop();
    }
  };
  ws.onopen = async () => {
    try {
      await peer.setLocalDescription(await peer.createOffer());
      if (peer.iceGatheringState !== "complete")
        await new Promise((resolve) => {
          peer.onicegatheringstatechange = () => {
            if (peer.iceGatheringState === "complete") resolve();
          };
        });
      if (focus === active)
        ws.send(JSON.stringify({ request, offer: peer.localDescription }));
    } catch {
      stop();
    }
  };
}
function preview(seat, binding, m, outputs, onDisconnect) {
  const request = {
    version: 1,
    kind: "preview.subscribe",
    subscriptionId: crypto.randomUUID(),
    nodeId: binding.nodeId,
    sessionId: binding.session.sessionId,
    layoutRevision: binding.session.layoutRevision,
    monitorId: m.monitorId,
  };
  const panel = document.createElement("button"),
    canvas = document.createElement("canvas"),
    label = document.createElement("span");
  canvas.width = 1;
  canvas.height = 1;
  const caption = document.createElement("span");
  caption.className = "caption";
  const identity = state.labels?.[seat.nodeId];
  caption.textContent = [identity?.hostname ?? seat.nodeId, identity?.user]
    .filter(Boolean)
    .join(" · ");
  label.className = "signal";
  function signal(text) {
    label.textContent = text;
    label.dataset.quiet = String(text === "в эфире" || text === "статично");
    panel.setAttribute("aria-description", caption.textContent + " · " + text);
    panel.title = caption.textContent + " · " + m.monitorId + " · " + text;
  }
  signal("ожидание");
  panel.append(canvas, caption, label);
  panel.className = "monitor";
  panel.setAttribute("aria-label", seat.seatId + " · " + m.monitorId);
  panel.onclick = () => openView(seat, m.monitorId);
  outputs.append(panel);
  let verified = 0,
    cancelled = false;
  const ws = socket();
  ws.binaryType = "arraybuffer";
  const receiver = new PreviewReceiver({
    paint: (image) => {
      canvas.width = image.width;
      canvas.height = image.height;
      canvas.getContext("2d").drawImage(image, 0, 0);
      verified = performance.now();
      signal("в эфире");
    },
    clear: () => {
      canvas.width = 1;
      canvas.height = 1;
      verified = 0;
    },
    ack: (ack) => ws.send(JSON.stringify(ack)),
  });
  receiver.subscribe(request, binding);
  ws.onopen = () => ws.send(JSON.stringify({ request }));
  ws.onmessage = async ({ data }) => {
    try {
      if (typeof data === "string") {
        const msg = JSON.parse(data);
        if (receiver.accept(msg) && msg.kind === "preview.verified") {
          verified = performance.now();
          signal("статично");
        }
      } else {
        const n = new DataView(data).getUint32(0);
        if (!n || n > 4096 || data.byteLength <= 4 + n) throw Error();
        const h = JSON.parse(new TextDecoder().decode(data.slice(4, 4 + n)));
        await receiver.frame(h, data.slice(4 + n), (bytes) =>
          createImageBitmap(new Blob([bytes], { type: "image/jpeg" })),
        );
      }
    } catch {
      ws.close();
    }
  };
  const timer = setInterval(() => {
    if (verified && performance.now() - verified > 2000) signal("кадр устарел");
  }, 500);
  ws.onclose = () => {
    receiver.reset();
    clearInterval(timer);
    signal("нет связи");
    if (!cancelled) onDisconnect();
  };
  return () => {
    cancelled = true;
    receiver.reset();
    clearInterval(timer);
    ws.close();
  };
}
function renderGrid() {
  const seats = state.room.seats;
  const grid = el("grid");
  const rows = [...new Set(seats.map((s) => s.row))].sort((a, b) => a - b);
  const cols = [...new Set(seats.map((s) => s.column))].sort((a, b) => a - b);
  grid.style.gridTemplateColumns = "repeat(" + cols.length + ", minmax(0,1fr))";
  grid.style.gridTemplateRows = "repeat(" + rows.length + ", minmax(0,1fr))";
  for (const seat of seats) {
    const binding = state.nodes[seat.nodeId];
    const sig = JSON.stringify([signature(seat), state.labels?.[seat.nodeId]]);
    const old = cards.get(seat.seatId);
    if (old?.signature === sig) continue;
    old?.stop();
    const card = document.createElement("article"),
      heading = document.createElement("h2");
    card.style.gridRow = rows.indexOf(seat.row) + 1;
    card.style.gridColumn = cols.indexOf(seat.column) + 1;
    heading.textContent = seat.seatId + " · " + (seat.nodeId ?? "нет ПК");
    if (!binding?.session) {
      heading.textContent =
        state.labels?.[seat.nodeId]?.hostname ?? seat.nodeId ?? seat.seatId;
      card.append(heading);
    }
    const outputs = document.createElement("div");
    outputs.className = "outputs";
    card.append(outputs);
    const stops = [];
    const entry = {
      signature: sig,
      stop() {
        stops.forEach((fn) => fn());
        card.remove();
      },
    };
    cards.set(seat.seatId, entry);
    for (const m of binding?.session?.monitors ?? []) {
      stops.push(
        preview(seat, binding, m, outputs, () => {
          entry.signature = null;
        }),
      );
    }
    if (!stops.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = !seat.nodeId
        ? "Место свободно"
        : binding?.availability === "online"
          ? "Нет графической сессии"
          : "ПК вне сети";
      outputs.append(p);
    }
    grid.append(card);
  }
  const ids = new Set(seats.map((s) => s.seatId));
  for (const [id, card] of cards)
    if (!ids.has(id)) {
      card.stop();
      cards.delete(id);
    }
}
async function poll() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (!response.ok) throw Error();
    const initial = !state;
    state = await response.json();
    document.title = state.room.roomId + " · classctl";
    if (initial) el("notice").textContent = "";
    if (focus) {
      const seat = state.room.seats.find((s) => s.seatId === focus.seat.seatId);
      if (!seat || signature(seat) !== focus.signature) closeFocus();
    }
    if (!focus) renderGrid();
  } catch {
    state = null;
    closeFocus();
    clearGrid();
    el("notice").textContent = "Аудитория недоступна";
  }
  setTimeout(poll, 600);
}
poll();
