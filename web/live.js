import { PreviewReceiver } from "/receiver.mjs";

import { bindInput } from "/input.js";
const el = (id) => document.getElementById(id);
let state, focus;
const cards = new Map();
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
  label.textContent = m.monitorId + " · ожидание";
  panel.append(canvas, label);
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
      label.textContent = m.monitorId + " · живое превью";
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
          label.textContent = m.monitorId + " · экран неподвижен";
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
    if (verified && performance.now() - verified > 2000)
      label.textContent = m.monitorId + " · кадр устарел";
  }, 500);
  ws.onclose = () => {
    receiver.reset();
    clearInterval(timer);
    label.textContent = m.monitorId + " · нет связи";
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
    const sig = signature(seat);
    const old = cards.get(seat.seatId);
    if (old?.signature === sig) continue;
    old?.stop();
    const card = document.createElement("article"),
      heading = document.createElement("h2");
    card.style.gridRow = rows.indexOf(seat.row) + 1;
    card.style.gridColumn = cols.indexOf(seat.column) + 1;
    heading.textContent = seat.seatId + " · " + (seat.nodeId ?? "нет ПК");
    card.append(heading);
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
