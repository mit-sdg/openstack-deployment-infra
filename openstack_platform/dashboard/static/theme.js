try {
  const key = document.currentScript?.dataset.themeStorage;
  const theme = key ? localStorage.getItem(key) : null;
  if (theme === "light" || theme === "dark")
    document.documentElement.dataset.theme = theme;
} catch {
  /* System preference remains available without browser storage. */
}
