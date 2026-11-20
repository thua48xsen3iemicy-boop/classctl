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
    "/stream",
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
  el("overview").hidden = false;
  return current.scroll;
}
function back() {
  const scroll = closeFocus();
  if (state) renderGrid();
  if (scroll) viewport.scrollTo(...scroll);
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
    state.wheelNodes?.includes(seat.nodeId),
  ]);
}
async function openView(seat, id) {
  const binding = state.nodes[seat.nodeId];
  const control = state.controlNodes?.includes(seat.nodeId) ?? false;
  const ws = socket(control),
    peer = new RTCPeerConnection({ iceServers: state.iceServers });
  const active = {
    ws, peer, seat, signature: signature(seat),
    scroll: [viewport.scrollLeft, viewport.scrollTop],
  };
  showChoices(false);
  if (!sidebarPinned) showSidebar(false);
  focus = active;
  el("overview").hidden = true;
  clearGrid();
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
      active.cleanup = bindInput(
        video, ws, state.wheelNodes?.includes(seat.nodeId),
      );
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
function preview(seat, binding, m, name, identity, outputs, onDisconnect) {
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
  label.className = "signal";
  function signal(text) {
    label.textContent = text;
    label.dataset.quiet = String(text === "в эфире" || text === "статично");
    panel.setAttribute("aria-description", identity + " · " + text);
    panel.title = identity + " · " + name + " · " + text;
  }
  signal("ожидание");
  panel.append(canvas, label);
  panel.className = "monitor";
  panel.type = "button";
  panel.setAttribute("aria-label", seat.seatId + " · " + name);
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
let hidden = new Set(), choiceSignature, storageKey;
const sidebar = el("sidebar");
const sidebarHandle = el("sidebar-handle");
const sidebarPanel = el("sidebar-panel");
const sidebarPin = el("sidebar-pin");
const sidebarPinKey = "classctl.sidebarPinned";
let sidebarPinned = false;

function showSidebar(open) {
  sidebar.classList.toggle("open", open);
  sidebarPanel.inert = !open;
  sidebarHandle.setAttribute("aria-expanded", String(open));
  sidebarHandle.textContent = open ? "‹" : "›";
}
function pinSidebar(pinned) {
  sidebarPinned = pinned;
  sidebar.classList.toggle("pinned", pinned);
  el("overview").toggleAttribute("data-sidebar-pinned", pinned);
  sidebarPin.setAttribute("aria-pressed", String(pinned));
  sidebarHandle.hidden = pinned;
  showSidebar(true);
}
try {
  if (localStorage.getItem(sidebarPinKey) === "true") pinSidebar(true);
} catch {}
sidebarPin.onclick = () => {
  pinSidebar(!sidebarPinned);
  try { localStorage.setItem(sidebarPinKey, String(sidebarPinned)); } catch {}
};
sidebarHandle.onclick = () => showSidebar(sidebarPanel.inert);
sidebar.onpointerenter = (event) => {
  if (event.pointerType === "mouse" && !sidebarPinned) showSidebar(true);
};
sidebar.onpointerleave = (event) => {
  if (
    event.pointerType === "mouse" &&
    !sidebarPinned &&
    !sidebarPanel.querySelector(":focus-visible")
  ) showSidebar(false);
};
sidebar.onfocusout = (event) => {
  if (!sidebarPinned && !sidebar.matches(":hover") && !sidebar.contains(event.relatedTarget))
    showSidebar(false);
};
document.addEventListener("pointerdown", (event) => {
  if (!sidebarPinned && !sidebar.contains(event.target)) showSidebar(false);
});

function showChoices(open) {
  el("choices-panel").hidden = !open;
  el("tab").setAttribute("aria-expanded", String(open));
}
el("tab").onclick = () => showChoices(el("choices-panel").hidden);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || focus || sidebarPanel.inert) return;
  if (!el("choices-panel").hidden) {
    showChoices(false);
    el("tab").focus();
  } else if (!sidebarPinned) {
    if (sidebarPanel.contains(document.activeElement)) sidebarHandle.focus();
    showSidebar(false);
  }
});

function renderGrid() {
  const key = "classctl.hiddenNodes." + state.room.roomId;
  if (key !== storageKey) {
    storageKey = key;
    hidden = new Set();
    try {
      const saved = JSON.parse(localStorage.getItem(key));
      if (Array.isArray(saved)) hidden = new Set(saved);
    } catch {}
  }
  const ordered = [...state.room.seats].sort(
    (a, b) => a.row - b.row || a.column - b.column,
  );
  const computers = ordered.filter((seat) => seat.nodeId);
  const visible = computers.filter((seat) => !hidden.has(seat.nodeId));
  updateChoices(computers, visible);
  const seats = ordered.filter((seat) => !hidden.has(seat.nodeId));
  el("tab").hidden = !computers.length;
  if (!computers.length) showChoices(false);
  const emptyMessage = "Все компьютеры скрыты. Выберите нужные в панели «Компьютеры».";
  if (computers.length && !visible.length) el("notice").textContent = emptyMessage;
  else if (el("notice").textContent === emptyMessage) el("notice").textContent = "";
  const grid = el("grid");
  grid.style.setProperty("--columns", Math.max(1, Math.ceil(Math.sqrt(seats.length))));
  grid.toggleAttribute("data-one-seat", seats.length === 1);
  const ids = new Set(seats.map((s) => s.seatId));
  for (const [id, entry] of cards)
    if (!ids.has(id)) {
      entry.stop();
      cards.delete(id);
    }
  let previous = null;
  for (const seat of seats) {
    const label = state.labels?.[seat.nodeId];
    const sig = JSON.stringify([signature(seat), label]);
    let entry = cards.get(seat.seatId);
    if (entry?.signature !== sig) {
      entry?.stop();
      entry = createCard(seat, state.nodes[seat.nodeId], label, sig);
      cards.set(seat.seatId, entry);
    }
    const next = previous ? previous.nextElementSibling : grid.firstElementChild;
    if (entry.card !== next) grid.insertBefore(entry.card, next);
    previous = entry.card;
  }
}
function createCard(seat, binding, label, sig) {
  const card = document.createElement("article"),
    heading = document.createElement("header"),
    caption = document.createElement("h2"),
    position = document.createElement("span"),
    outputs = document.createElement("div");
  const identity = [label?.hostname ?? seat.nodeId ?? seat.seatId, label?.user]
    .filter(Boolean)
    .join(" · ");
  card.className = "seat";
  heading.className = "seat-heading";
  caption.className = "caption";
  caption.textContent = caption.title = identity;
  heading.append(caption);
  if (seat.nodeId) {
    position.className = "seat-id";
    position.textContent = position.title = seat.seatId;
    heading.append(position);
  }
  outputs.className = "outputs";
  card.append(heading, outputs);
  const stops = [];
  const entry = {
    card,
    signature: sig,
    stop() {
      stops.forEach((fn) => fn());
      card.remove();
    },
  };
  const monitors = binding?.session?.monitors ?? [];
  outputs.style.setProperty("--outputs", monitors.length || 1);
  for (const [index, monitor] of monitors.entries()) {
    stops.push(
      preview(seat, binding, monitor, "Экран " + (index + 1), identity, outputs, () => {
        entry.signature = null;
      }),
    );
  }
  if (!monitors.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = !seat.nodeId
      ? "Место свободно"
      : binding?.availability === "online"
        ? "Нет графической сессии"
        : "ПК вне сети";
    outputs.append(p);
  }
  return entry;
}
function updateChoices(seats, visible) {
  const next = JSON.stringify([storageKey, seats, state.labels]);
  if (next !== choiceSignature) {
    el("choices").replaceChildren();
    for (const seat of seats) {
      const label = document.createElement("label");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = !hidden.has(seat.nodeId);
      check.onchange = () => {
        if (check.checked) hidden.delete(seat.nodeId);
        else hidden.add(seat.nodeId);
        try { localStorage.setItem(storageKey, JSON.stringify([...hidden])); } catch {}
        renderGrid();
      };
      label.append(check, seat.seatId + " · " + (state.labels?.[seat.nodeId]?.hostname ?? seat.nodeId));
      el("choices").append(label);
    }
    choiceSignature = next;
  }
  el("count").textContent = "Показано " + visible.length + " из " + seats.length;
}
async function poll() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (!response.ok) throw Error();
    const initial = !state;
    state = await response.json();
    document.title = state.room.roomId + " · classctl";
    el("room-name").textContent = state.room.roomId;
    if (initial) el("notice").textContent = "";
    if (focus) {
      const seat = state.room.seats.find((s) => s.seatId === focus.seat.seatId);
      if (!seat || signature(seat) !== focus.signature) back();
    } else renderGrid();
  } catch {
    state = null;
    showChoices(false);
    el("tab").hidden = true;
    closeFocus();
    clearGrid();
    el("room-name").textContent = "";
    el("notice").textContent = "Аудитория недоступна";
  }
  setTimeout(poll, 600);
}
poll();
