const keys = await (await fetch("/input-keys.json")).json();
export function coordinates(rect, width, height, x, y) {
  if (!width || !height) return null;
  const byWidth = rect.width / width <= rect.height / height;
  const w = byWidth ? rect.width : (rect.height * width) / height;
  const h = byWidth ? (rect.width * height) / width : rect.height;
  const u = (x - rect.left - (rect.width - w) / 2) / w;
  const v = (y - rect.top - (rect.height - h) / 2) / h;
  return u >= 0 && v >= 0 && u < 1 && v < 1 ? { u, v } : null;
}
export function bindInput(video, ws) {
  const abort = new AbortController();
  const options = { signal: abort.signal };
  function send(data) {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data));
  }
  function release() {
    send({ kind: "release" });
  }
  video.tabIndex = 0;
  video.style.objectFit = "contain";
  video.focus();
  for (const type of ["keydown", "keyup"]) {
    video.addEventListener(
      type,
      (e) => {
        if (!Object.hasOwn(keys, e.code)) return;
        e.preventDefault();
        if (!e.repeat)
          send({ kind: "key", down: type === "keydown", code: e.code });
      },
      options,
    );
  }
  for (const type of ["pointermove", "pointerdown", "pointerup"]) {
    video.addEventListener(
      type,
      (e) => {
        const point = coordinates(
          video.getBoundingClientRect(),
          video.videoWidth,
          video.videoHeight,
          e.clientX,
          e.clientY,
        );
        if (!point) {
          if (type === "pointerup") release();
          return;
        }
        if (type === "pointermove") {
          send({ kind: "move", ...point });
          return;
        }
        if (e.button < 0 || e.button > 2) return;
        e.preventDefault();
        if (type === "pointerdown") {
          video.focus();
          video.setPointerCapture(e.pointerId);
        }
        send({
          kind: "button",
          button: e.button,
          down: type === "pointerdown",
          ...point,
        });
      },
      options,
    );
  }
  video.addEventListener("contextmenu", (e) => e.preventDefault(), options);
  for (const event of ["blur", "pointercancel"])
    video.addEventListener(event, release, options);
  window.addEventListener("blur", release, options);
  document.addEventListener(
    "visibilitychange",
    () => {
      if (document.hidden) release();
    },
    options,
  );
  const timer = setInterval(() => send({ kind: "ping" }), 500);
  return () => {
    abort.abort();
    clearInterval(timer);
    release();
  };
}
