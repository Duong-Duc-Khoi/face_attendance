(function () {
  const KEY = "faceAttendTheme";
  const root = document.documentElement;

  function storedTheme() {
    let value = "";
    try {
      value = localStorage.getItem(KEY);
    } catch (_err) {
      value = "";
    }
    return value === "light" || value === "dark" ? value : "dark";
  }

  function persistTheme(theme) {
    try {
      localStorage.setItem(KEY, theme);
    } catch (_err) {}
  }

  function updateButtons(theme) {
    document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
      btn.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
      const label = btn.querySelector("[data-theme-label]");
      if (label) label.textContent = theme === "light" ? "Sáng" : "Tối";
      btn.title = theme === "light" ? "Đổi sang giao diện tối" : "Đổi sang giao diện sáng";
    });
  }

  function setTheme(theme) {
    const next = theme === "light" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    root.style.colorScheme = next;
    persistTheme(next);
    updateButtons(next);
    window.dispatchEvent(new CustomEvent("faceattend:themechange", { detail: { theme: next } }));
  }

  function toggleTheme() {
    setTheme(root.getAttribute("data-theme") === "light" ? "dark" : "light");
  }

  setTheme(storedTheme());

  document.addEventListener("DOMContentLoaded", () => {
    updateButtons(root.getAttribute("data-theme") || "dark");
    document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
      btn.addEventListener("click", toggleTheme);
    });
  });

  window.FaceAttendTheme = {
    get: () => root.getAttribute("data-theme") || "dark",
    set: setTheme,
    toggle: toggleTheme,
  };
})();
