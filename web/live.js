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
function closeConnection(connection) {
  connection.cleanup?.();
  connection.ws.close();
  connection.peer.close();
  connection.video.remove();
}
function closeFocus() {
  if (!focus) return;
  const current = focus;
  focus = null;
  setDrawer(false);
  if (document.fullscreenElement === el("focus"))
    document.exitFullscreen().catch(() => {});
  if (current.connection) closeConnection(current.connection);
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
  el("handle").textContent = open ? "‹" : "›";
  el("drawer").inert = !open;
}
el("tools").onpointerenter = (e) => {
  if (e.pointerType === "mouse") setDrawer(true);
};
el("tools").onpointerleave = (e) => {
  if (
    e.pointerType === "mouse" &&
    !el("drawer").querySelector(":focus-visible")
  )
    setDrawer(false);
};
el("handle").onclick = (e) =>
  setDrawer(
    e.pointerType === "mouse" || !el("tools").classList.contains("open"),
  );
el("tools").onfocusout = (e) => {
  if (!el("tools").contains(e.relatedTarget) && !el("tools").matches(":hover"))
    setDrawer(false);
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
function openView(seat, id) {
  const view = {
    seat, signature: signature(seat),
    scroll: [viewport.scrollLeft, viewport.scrollTop],
  };
  closePowerMenu();
  showChoices(false);
  if (!sidebarPinned) showSidebar(false);
  focus = view;
  el("overview").hidden = true;
  clearGrid();
  el("focus").hidden = false;
  // The browser requires user activation here, before any network await.
  el("focus")
    .requestFullscreen?.()
    .then(() => {
      view.fullscreen = true;
    })
    .catch(() => {});
  el("notice").textContent = "";
  connectMonitor(view, id);
}
function connectMonitor(view, id) {
  const { seat } = view;
  const binding = state.nodes[seat.nodeId];
  const control = state.controlNodes?.includes(seat.nodeId) ?? false;
  const ws = socket(control);
  const peer = new RTCPeerConnection({ iceServers: state.iceServers });
  const video = document.createElement("video");
  const active = { ws, peer, video, id };
  const isCurrent = () => focus === view && view.connection === active;
  view.connection = active;
  el("swap").hidden = binding.session.monitors.length !== 2;
  el("swap").removeAttribute("aria-disabled");
  el("target").textContent = seat.nodeId + " · " + id;
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
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  video.onplaying = () => {
    if (!isCurrent() || active.playing) return;
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
    if (!isCurrent()) return;
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
      if (isCurrent())
        ws.send(JSON.stringify({ request, offer: peer.localDescription }));
    } catch {
      stop();
    }
  };
}
el("swap").onclick = async () => {
  const view = focus;
  const current = view?.connection;
  if (!current) return;
  const monitors = state.nodes[view.seat.nodeId].session.monitors;
  if (monitors.length !== 2) return;
  const other = monitors.find((monitor) => monitor.monitorId !== current.id);
  view.connection = null;
  el("swap").setAttribute("aria-disabled", "true");
  el("mode").textContent = "Подключение…";
  // The coordinator acknowledges closure after releasing exclusive control.
  const closed = new Promise((resolve) =>
    current.ws.addEventListener("close", resolve, { once: true }),
  );
  closeConnection(current);
  await closed;
  if (focus === view) connectMonitor(view, other.monitorId);
};
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
  panel.setAttribute("aria-label", seat.nodeId + " · " + name);
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

const grid = el("grid");
const powerPanel = el("power");
const powerMenu = el("power-menu");
const powerStatus = el("power-status");
const powerButtons = [...powerPanel.querySelectorAll("button")];
const menuButtons = [...powerMenu.querySelectorAll("button")];
const selectedNodes = new Set();
const powerMessages = {
  "handed-off": "Команда передана системе",
  offline: "Компьютер вне сети",
  unsupported: "Действие недоступно на этом компьютере",
  busy: "Предыдущая команда ещё действует",
  expired: "Срок команды истёк",
  "invalid-clock": "Проверьте время на компьютере",
  "system-error": "Система отклонила команду",
  unconfirmed: "Выполнение команды не подтверждено",
};
let powerPending = false, menuTargets = [], menuTrigger;

function renderPower() {
  const nodes = state?.powerNodes;
  const available = Array.isArray(nodes);
  powerPanel.hidden = !available;
  if (!available) {
    selectedNodes.clear();
    closePowerMenu();
  }
  for (const button of powerButtons) button.disabled = powerPending || !nodes?.length;
  const menuDisabled = powerPending || !menuTargets.some((id) => nodes?.includes(id));
  for (const button of menuButtons) button.disabled = menuDisabled;
}

function formatPowerResult(outcomes) {
  if (outcomes.length === 1) {
    const { nodeId, reason } = outcomes[0];
    return `${nodeId} · ${powerMessages[reason] || "Действие отклонено"}`;
  }
  const failures = new Map();
  let succeeded = 0;
  for (const { state, reason } of outcomes) {
    if (state === "succeeded") succeeded++;
    else failures.set(reason, (failures.get(reason) || 0) + 1);
  }
  return [
    `Команда передана системе: ${succeeded} из ${outcomes.length}`,
    ...Array.from(failures, ([reason, count]) => `${powerMessages[reason] || "Действие отклонено"}: ${count}`),
  ].join(" · ");
}

async function sendPower(button, nodeIds, scope) {
  if (powerPending || !nodeIds.some((id) => state?.powerNodes?.includes(id))) return;
  if (!window.confirm(`${button.textContent} ${scope}? Несохранённые данные могут быть потеряны.`)) return;
  powerPending = true;
  powerStatus.textContent = "Отправка команды…";
  renderPower();
  try {
    const response = await fetch("/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actionId: button.dataset.action, nodeIds }),
    });
    if (response.status === 403) {
      powerStatus.textContent = "Недостаточно прав";
      return;
    }
    if (!response.ok) throw Error();
    const { outcomes } = await response.json();
    powerStatus.textContent = formatPowerResult(outcomes);
  } catch {
    powerStatus.textContent = "Выполнение команды не подтверждено";
  } finally {
    powerPending = false;
    renderPower();
  }
}

for (const button of powerButtons) button.onclick = () => {
  const nodeIds = [...(state?.powerNodes ?? [])];
  sendPower(button, nodeIds, `аудитории (${nodeIds.length} доступно)`);
};

function reflectSelection(card) {
  const id = card.dataset.nodeId;
  const selected = selectedNodes.has(id);
  card.toggleAttribute("data-selected", selected);
  card.setAttribute("aria-label", selected ? `${id}, выбран` : id);
}

function toggleSelection(card) {
  const id = card.dataset.nodeId;
  if (selectedNodes.has(id)) selectedNodes.delete(id);
  else selectedNodes.add(id);
  reflectSelection(card);
}

function openPowerMenu(card, x, y) {
  const id = card.dataset.nodeId;
  menuTargets = selectedNodes.has(id) ? [...selectedNodes] : [id];
  menuTrigger = card.contains(document.activeElement) ? document.activeElement : card;
  el("power-menu-label").textContent = menuTargets.length === 1 ? id : `Выбрано ${menuTargets.length} ПК`;
  powerMenu.hidden = false;
  renderPower();
  powerMenu.style.left = `${Math.max(0, Math.min(x, innerWidth - powerMenu.offsetWidth))}px`;
  powerMenu.style.top = `${Math.max(0, Math.min(y, innerHeight - powerMenu.offsetHeight))}px`;
  (menuButtons[0].disabled ? powerMenu : menuButtons[0]).focus({ preventScroll: true });
}

function closePowerMenu(restoreFocus = false) {
  if (powerMenu.hidden) return;
  powerMenu.hidden = true;
  if (restoreFocus) menuTrigger?.focus({ preventScroll: true });
  menuTargets = [];
  menuTrigger = null;
}

for (const button of menuButtons) button.onclick = () => {
  const targets = menuTargets;
  closePowerMenu(true);
  sendPower(button, targets, targets.join(", "));
};

grid.addEventListener("click", (event) => {
  const card = event.target.closest(".seat[data-node-id]");
  if (!card || !event.ctrlKey || powerPanel.hidden) return;
  event.preventDefault();
  event.stopPropagation();
  toggleSelection(card);
  card.focus({ preventScroll: true });
}, true);
grid.addEventListener("contextmenu", (event) => {
  const card = event.target.closest(".seat[data-node-id]");
  if (!card || powerPanel.hidden) return;
  event.preventDefault();
  openPowerMenu(card, event.clientX, event.clientY);
});
grid.addEventListener("keydown", (event) => {
  const card = event.target.closest(".seat[data-node-id]");
  if (!card || powerPanel.hidden) return;
  if (event.ctrlKey && event.code === "Space") {
    event.preventDefault();
    if (!event.repeat) toggleSelection(card);
  } else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
    event.preventDefault();
    const { left, top } = card.getBoundingClientRect();
    openPowerMenu(card, left, top);
  }
});
powerMenu.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.stopPropagation();
    closePowerMenu(true);
  } else if (["ArrowDown", "ArrowUp"].includes(event.key) && !menuButtons[0].disabled) {
    event.preventDefault();
    menuButtons[document.activeElement === menuButtons[0] ? 1 : 0].focus();
  }
});
document.addEventListener("pointerdown", (event) => {
  if (!powerMenu.contains(event.target)) closePowerMenu();
});
document.addEventListener("focusin", (event) => {
  if (!powerMenu.contains(event.target)) closePowerMenu();
});
viewport.addEventListener("scroll", () => closePowerMenu());
window.addEventListener("resize", () => closePowerMenu());

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
    selectedNodes.clear();
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
  const visibleIds = new Set(visible.map((seat) => seat.nodeId));
  for (const id of selectedNodes) if (!visibleIds.has(id)) selectedNodes.delete(id);
  if (menuTargets.some((id) => !visibleIds.has(id))) closePowerMenu();
  updateChoices(computers, visible);
  const seats = ordered.filter((seat) => !hidden.has(seat.nodeId));
  el("tab").hidden = !computers.length;
  if (!computers.length) showChoices(false);
  const emptyMessage = "Все компьютеры скрыты. Выберите нужные в панели «Компьютеры».";
  if (computers.length && !visible.length) el("notice").textContent = emptyMessage;
  else if (el("notice").textContent === emptyMessage) el("notice").textContent = "";
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
    const displayName = state.labels?.[seat.nodeId]?.displayName;
    const sig = JSON.stringify([signature(seat), displayName]);
    let entry = cards.get(seat.seatId);
    if (entry?.signature !== sig) {
      entry?.stop();
      entry = createCard(seat, state.nodes[seat.nodeId], displayName, sig);
      cards.set(seat.seatId, entry);
    }
    if (seat.nodeId) {
      if (powerPanel.hidden) entry.card.removeAttribute("tabindex");
      else entry.card.tabIndex = 0;
      reflectSelection(entry.card);
    }
    const next = previous ? previous.nextElementSibling : grid.firstElementChild;
    if (entry.card !== next) grid.insertBefore(entry.card, next);
    previous = entry.card;
  }
}
function createCard(seat, binding, displayName, sig) {
  const card = document.createElement("article"),
    outputs = document.createElement("div");
  const identity = [seat.nodeId, displayName]
    .filter(Boolean)
    .join(" · ");
  card.className = "seat";
  if (seat.nodeId) {
    card.dataset.nodeId = seat.nodeId;
    const caption = document.createElement("h2");
    const computer = document.createElement("span");
    caption.className = "caption";
    caption.title = identity;
    computer.className = "caption-node";
    computer.textContent = seat.nodeId;
    caption.append(computer);
    if (displayName) {
      const student = document.createElement("span");
      student.textContent = " · " + displayName;
      caption.append(student);
    }
    card.append(caption);
  }
  outputs.className = "outputs";
  card.append(outputs);
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
  const next = JSON.stringify([storageKey, seats]);
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
      label.append(check, seat.nodeId);
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
    renderPower();
    document.title = state.room.roomId + " · classctl";
    el("room-name").textContent = state.room.roomId;
    if (initial) el("notice").textContent = "";
    if (focus) {
      const seat = state.room.seats.find((s) => s.seatId === focus.seat.seatId);
      if (!seat || signature(seat) !== focus.signature) back();
    } else renderGrid();
  } catch {
    state = null;
    renderPower();
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
