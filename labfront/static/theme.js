/* Day or dark, picked in the head so the first paint is already right.
   LabFront follows the computer's light or dark setting. The switch in the top
   bar picks the other theme and remembers it; switching back to the theme the
   computer already uses forgets the choice, so LabFront follows it again. */
(() => {
  const root = document.documentElement, KEY = "lf_theme";
  const light = matchMedia("(prefers-color-scheme: light)");
  const system = () => (light.matches ? "day" : "dark");
  const chosen = () => {
    try { const t = localStorage.getItem(KEY); return t === "day" || t === "dark" ? t : null; } catch { return null; }
  };
  const label = () => {
    const btn = document.getElementById("themebtn");
    if (!btn) return;
    const text = root.dataset.theme === "day" ? "Switch to dark theme" : "Switch to day theme";
    btn.setAttribute("aria-label", text);
    btn.title = text;
  };
  const apply = t => {
    if (root.dataset.theme === t) return label();
    root.classList.add("theme-swap");
    root.dataset.theme = t;
    label();
    requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("theme-swap")));
  };

  apply(chosen() || system());
  light.addEventListener("change", () => { if (!chosen()) apply(system()); });
  addEventListener("storage", e => { if (e.key === KEY || e.key === null) apply(chosen() || system()); });
  document.addEventListener("DOMContentLoaded", () => {
    label();
    document.getElementById("themebtn")?.addEventListener("click", () => {
      const next = root.dataset.theme === "day" ? "dark" : "day";
      try { next === system() ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, next); } catch { /* private mode: still switch */ }
      apply(next);
    });
  });
})();
