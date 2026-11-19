/* No passwords/session credentials in Web Storage. window.name holds only the
   tab binding; the required primary session credential is an HttpOnly cookie. */
(() => {
  if (window.__tabAccessRunning) return;
  window.__tabAccessRunning = true;
  const bootStatus = text => { const element = document.querySelector('#signin-status'); if (element) element.textContent = text; };
  window.addEventListener('error', event => bootStatus('Tab verification could not start: ' + event.message));
  window.addEventListener('unhandledrejection', event => bootStatus('Tab verification could not start: ' + (event.reason?.message || 'Browser capability unavailable.')));
  bootStatus('Checking this tab…');
  const config = JSON.parse(document.querySelector('#auth-config').textContent);
  const prefix = 'shygarma-tab:';
  const navigationType = window.performance?.getEntriesByType?.('navigation')[0]?.type;
  let binding;
  let releaseLock;
  let unlocked = false;
  const headers = () => ({'Content-Type':'application/json', 'X-CSRF-Token':config.csrf, 'X-Tab-Token':binding.id});
  const persist = () => { window.name = prefix + JSON.stringify(binding); };
  const reset = () => {
    const random = new Uint8Array(24);
    window.crypto.getRandomValues(random);
    binding = {id:Array.from(random, byte => byte.toString(16).padStart(2,'0')).join('')}; persist();
  };
  const status = text => { const element = document.querySelector('#signin-status'); if (element) element.textContent = text; };
  const call = async (url, options={}) => {
    const response = await fetch(url, {cache:'no-store', credentials:'same-origin', ...options, headers:headers()});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || 'Verify your password to continue.');
    if (result.csrf) config.csrf = result.csrf;
    return result;
  };
  try { const name = String(window.name || ''); binding = JSON.parse(name.startsWith(prefix) ? name.slice(prefix.length) : 'null'); } catch { binding = null; }
  if (!binding || typeof binding.id !== 'string') reset();

  async function ownBinding() {
    // Without ownership support, require verification rather than reuse a
    // binding that may have been copied into a second tab by the browser.
    if (!navigator.locks) return false;
    return new Promise(resolve => {
      navigator.locks.request('shygarma-' + binding.id, {ifAvailable:true}, async lock => {
        resolve(Boolean(lock));
        if (lock) await new Promise(done => {releaseLock = done;});
      }).catch(() => resolve(false));
    });
  }

  async function openWorkspace() {
    status('Opening your articles…');
    const response = await fetch('/shygarma/workspace', {headers:headers(),cache:'no-store',credentials:'same-origin'});
    if (!response.ok) throw new Error('Session expired. Verify again.');
    const html = await response.text();
    // This tab's document is replaced only with authorized, server-rendered HTML.
    // Release the ownership lock before the new bootstrap reacquires it.
    if (releaseLock) releaseLock();
    window.__tabAccessRunning = false;
    window.__shygarmaContinuation = binding.id;
    document.open(); document.write(html); document.close();
  }

  function ready() {
    unlocked = true;
    const navigation = document.querySelector('.primary-nav');
    if (!navigation.querySelector('.write-link')) {
      const link = document.createElement('a');
      link.href = '/shygarma'; link.className = 'write-link'; link.textContent = 'Write post';
      navigation.append(link);
    }
  }

  document.addEventListener('click', async event => {
    const link = event.target.closest('a');
    if (!unlocked || !link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target || link.download) return;
    const destination = new URL(link.href, location.href);
    if (destination.origin !== location.origin || destination.pathname === location.pathname && destination.hash) return;
    event.preventDefault();
    try {
      const result = await call('/shygarma/navigation', {method:'POST',body:'{}'});
      binding.ticket = result.ticket; persist();
      if (releaseLock) releaseLock();
      location.assign(destination.href);
    } catch { window.name = ''; location.assign(destination.href); }
  });

  document.querySelector('#signin-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const password = document.querySelector('#admin-password');
    const button = event.target.querySelector('button');
    button.disabled = true; status('Verifying…');
    try {
      await call('/shygarma/login', {method:'POST',body:JSON.stringify({password:password.value,tab_id:binding.id})});
      password.value = ''; persist();
      await openWorkspace();
    } catch (error) { status(error.message); password.value = ''; button.disabled = false; }
  });

  (async () => {
    const continuation = window.__shygarmaContinuation === binding.id;
    delete window.__shygarmaContinuation;
    const ticket = binding.ticket;
    delete binding.ticket;
    if (!continuation && navigationType !== 'reload' && !ticket) reset();
    if (!(await ownBinding())) {
      reset();
      // Own the replacement too, so a manually verified second tab is also
      // protected against a later browser copy of its new binding.
      await ownBinding();
    }
    persist();
    try {
      if (ticket && !continuation) await call('/shygarma/resume', {method:'POST',body:JSON.stringify({ticket,tab_id:binding.id})});
      else if (navigationType === 'reload' || continuation) await call('/shygarma/status');
      else { status('Verify your password to unlock this tab.'); return; }
      if (config.gate) await openWorkspace();
      else ready();
    } catch { if (config.gate) status('Verify your password to unlock this tab.'); }
  })();
  addEventListener('pagehide', () => { if (releaseLock) releaseLock(); });
  addEventListener('pageshow', event => { if (event.persisted) { window.name = ''; location.reload(); } });
})();
