import { PreviewReceiver } from "/receiver.mjs";

const el = (id) => document.getElementById(id);
let signature,
  binding,
  grid = [],
  focus,
  ice = [];
const socket = () =>
  new WebSocket("wss://" + location.host + "/stream", "classctl.preview.v1");
function button(label, action) {
  const b = document.createElement("button");
  b.textContent = label;
  b.onclick = action;
  el("actions").append(b);
}
function closeFocus() {
  if (focus) {
    const { ws, peer } = focus;
    focus = null;
    ws.close();
    peer.close();
  }
  el("focus").replaceChildren();
  el("grid").hidden = false;
  el("grid").style.display = "flex";
}
function stopGrid() {
  grid.forEach((stop) => stop());
  grid = [];
  el("grid").replaceChildren();
}
function startGrid() {
  stopGrid();
  grid = (binding?.session?.monitors ?? []).map(preview);
}
function controls() {
  el("actions").replaceChildren();
  if (focus) {
    button("Вернуться в грид", () => {
      closeFocus();
      startGrid();
      controls();
    });
    return;
  }
  for (const m of binding?.session?.monitors ?? [])
    button("Открыть " + m.monitorId, () => openView(m.monitorId));
  if (binding?.session) button("Весь рабочий стол", () => openView(null));
}
async function openView(id) {
  closeFocus();
  stopGrid();
  const ws = socket(),
    peer = new RTCPeerConnection({ iceServers: ice });
  const active = { ws, peer };
  focus = active;
  const request = {
    version: 1,
    kind: "access",
    nodeId: binding.nodeId,
    sessionId: binding.session.sessionId,
    layoutRevision: binding.session.layoutRevision,
    mode: "view",
    target: id ? { scope: "monitor", monitorId: id } : { scope: "desktop" },
  };
  const title = document.createElement("h3");
  title.textContent = id ?? "Весь рабочий стол";
  const video = document.createElement("video");
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  video.style.cssText = "max-width:100%;max-height:65vh";
  el("focus").append(title, video);
  el("grid").style.display = "none";
  controls();
  const stop = () => {
    if (focus === active) {
      closeFocus();
      startGrid();
      controls();
      el("status").textContent = "Просмотр завершён";
    }
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
function preview(m) {
  const request = {
    version: 1,
    kind: "preview.subscribe",
    subscriptionId: crypto.randomUUID(),
    nodeId: binding.nodeId,
    sessionId: binding.session.sessionId,
    layoutRevision: binding.session.layoutRevision,
    monitorId: m.monitorId,
  };
  const panel = document.createElement("div"),
    canvas = document.createElement("canvas"),
    label = document.createElement("p");
  canvas.width = 1;
  canvas.height = 1;
  label.textContent = m.monitorId + " · ожидание";
  panel.append(canvas, label);
  el("grid").append(panel);
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
    if (!cancelled) signature = null;
  };
  return () => {
    cancelled = true;
    receiver.reset();
    clearInterval(timer);
    ws.close();
  };
}
function reset() {
  stopGrid();
  closeFocus();
}
async function poll() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (!response.ok) throw Error();
    const state = await response.json(),
      seat = state.room.seats[0];
    binding = state.nodes[seat.nodeId];
    ice = state.iceServers;
    el("room").textContent = "Аудитория " + state.room.roomId;
    el("seat").textContent = seat.seatId + " · " + seat.nodeId;
    const next = JSON.stringify([
      seat,
      binding?.availability,
      binding?.session,
    ]);
    if (next !== signature) {
      reset();
      signature = next;
      el("status").textContent = binding?.session
        ? "Просмотр доступен"
        : binding?.availability === "online"
          ? "Нет графической сессии"
          : "ПК вне сети";
      startGrid();
      controls();
    }
  } catch {
    reset();
    signature = null;
    binding = null;
    controls();
    el("status").textContent = "Аудитория недоступна";
  }
  setTimeout(poll, 600);
}
poll();
