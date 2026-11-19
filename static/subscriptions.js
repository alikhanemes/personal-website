/* Progressive enhancement; ordinary POST forms remain available without JS. */
document.addEventListener('submit', async (event) => {
  const form = event.target;
  if (!form.matches('[data-subscription-form]')) return;
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch(form.action, {
      method: 'POST', body: new FormData(form), credentials: 'same-origin', cache: 'no-store',
    });
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const content = page.querySelector('#content');
    if (!content) throw new Error('Please refresh the page and try again.');
    document.querySelector('#content').replaceWith(content);
    document.title = page.title;
    content.tabIndex = -1;
    content.focus();
  } catch (error) {
    let message = form.querySelector('[role="alert"]');
    if (!message) { message = document.createElement('p'); message.setAttribute('role', 'alert'); form.append(message); }
    message.textContent = 'The request could not be completed. Please refresh the page and try again.';
    button.disabled = false;
  }
});
