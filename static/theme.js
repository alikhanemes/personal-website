/* Only the display preference is stored; authentication stays separate. Run in
   the head so a saved dark theme is applied before the stylesheet paints. */
(() => {
  window.__blogThemeEvents?.abort();
  const events = new AbortController();
  window.__blogThemeEvents = events;
  const options = {signal: events.signal};
  const root = document.documentElement;
  const valid = value => value === 'dark' || value === 'light';
  let preference;
  try { preference = localStorage.getItem('blog-theme'); } catch { /* Storage may be unavailable in a preview or private browser. */ }

  const preview = () => document.getElementById('preview-frame')?.contentWindow;
  const syncPreview = () => preview()?.postMessage({type: 'blog-theme-sync', theme: root.dataset.theme}, '*');
  function apply(theme, animate = false) {
    if (animate) root.classList.add('theme-transition');
    root.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#17191c' : '#ffffff');
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.hidden = false;
      button.setAttribute('aria-pressed', String(theme === 'dark'));
      button.title = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    });
    syncPreview();
  }
  function choose(theme) {
    preference = theme;
    try { localStorage.setItem('blog-theme', theme); } catch { /* The toggle still works without persistence. */ }
    apply(theme, true);
    if (window.parent !== window) window.parent.postMessage({type: 'blog-theme-change', theme}, '*');
  }
  apply(valid(preference) ? preference : 'light');
  document.addEventListener('DOMContentLoaded', () => apply(root.dataset.theme), options);
  document.addEventListener('click', event => {
    if (event.target instanceof Element && event.target.closest('[data-theme-toggle]')) {
      choose(root.dataset.theme === 'dark' ? 'light' : 'dark');
    }
  }, options);
  window.addEventListener('storage', event => {
    if (event.key !== 'blog-theme' && event.key !== null) return;
    preference = event.newValue;
    apply(valid(preference) ? preference : 'light', true);
  }, options);
  // A sandboxed private preview cannot read localStorage. Exchange only the
  // theme with that specific frame; no article or account data crosses here.
  window.addEventListener('message', event => {
    if (window.parent !== window && event.source === window.parent && event.data?.type === 'blog-theme-sync' && valid(event.data.theme)) {
      apply(event.data.theme, root.dataset.theme !== event.data.theme);
    } else if (preview() && event.source === preview()) {
      if (event.data?.type === 'blog-theme-request') syncPreview();
      else if (event.data?.type === 'blog-theme-change' && valid(event.data.theme)) choose(event.data.theme);
    }
  }, options);
  if (window.parent !== window) window.parent.postMessage({type: 'blog-theme-request'}, '*');
})();
