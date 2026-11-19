document.addEventListener('click', event => {
  const spoiler = event.target instanceof Element ? event.target.closest('button.spoiler') : null;
  if (!spoiler) return;
  const revealed = spoiler.getAttribute('aria-expanded') !== 'true';
  spoiler.setAttribute('aria-expanded', String(revealed));
  spoiler.setAttribute('aria-label', revealed ? 'Conceal spoiler: ' + spoiler.textContent : 'Reveal spoiler');
});
