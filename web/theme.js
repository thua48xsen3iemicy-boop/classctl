(() => {
  const root = document.documentElement;
  const systemDark = matchMedia("(prefers-color-scheme: dark)");
  const storageKey = "classctl.theme";
  let preference;
  let buttons = [];
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored === "light" || stored === "dark") preference = stored;
  } catch {}

  function applyTheme() {
    const theme = preference ?? (systemDark.matches ? "dark" : "light");
    root.dataset.theme = theme;
    const label = theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему";
    for (const button of buttons) {
      button.setAttribute("aria-label", label);
      button.title = label;
    }
  }

  applyTheme();
  systemDark.addEventListener("change", () => {
    if (!preference) applyTheme();
  });
  document.addEventListener("DOMContentLoaded", () => {
    buttons = document.querySelectorAll(".theme-toggle");
    for (const button of buttons) button.onclick = () => {
      preference = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem(storageKey, preference); } catch {}
      applyTheme();
    };
    applyTheme();
  }, { once: true });
})();
