// Apply a saved light or dark preference before first paint. Without one, the
// stylesheet follows prefers-color-scheme.
(function () {
  try {
    var theme = window.localStorage.getItem("platform-dashboard-theme");
    if (theme === "light" || theme === "dark") {
      document.documentElement.dataset.theme = theme;
    }
  } catch (error) {
    // Storage can be unavailable in hardened browser profiles.
  }
})();
