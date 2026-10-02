try {
  const theme = localStorage.getItem('owner-portal-theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
} catch {
  /* System preference remains available without browser storage. */
}
