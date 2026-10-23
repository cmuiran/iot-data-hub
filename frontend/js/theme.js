// Light / dark / follow-the-system theme, remembered per browser.

const KEY = 'hub.theme';
const media = window.matchMedia('(prefers-color-scheme: dark)');

export function themeSetting() {
  try { return localStorage.getItem(KEY) || 'system'; } catch { return 'system'; }
}

export function applyTheme(setting = themeSetting()) {
  const dark = setting === 'dark' || (setting === 'system' && media.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}

export function setTheme(setting) {
  try { localStorage.setItem(KEY, setting); } catch { /* storage blocked */ }
  applyTheme(setting);
}

media.addEventListener('change', () => applyTheme());
