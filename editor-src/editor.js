import { Editor, Mark, Node, mergeAttributes } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { EditorState, NodeSelection } from '@tiptap/pm/state';

// The server supplies this capability only after this tab has been verified.
// It remains in this document's memory, never localStorage or sessionStorage.
const config = window.EDITOR_CONFIG;
delete window.EDITOR_CONFIG;
const $ = (selector) => document.querySelector(selector);
const emptyBody = () => ({ type: 'doc', content: [{ type: 'paragraph' }] });
const apiBase = config.apiBase || '/shygarma/api';
let current = null;
let articles = config.posts || [];
let revision = 0;
let savedRevision = 0;
let savePromise = null;
let saveTimer;
let conflicted = false;
let busy = false;
let loading = false;
let pendingStateAction = null;
let notificationTimer = null;
let notificationRequest = null;
let notificationGeneration = 0;
let notificationPollsLeft = 36;
let lastNotificationStatus = null;
const mediaCache = new Map();
const mediaBlobs = new Map();
const objectUrls = new Set();

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('X-CSRF-Token', config.csrf);
  headers.set('X-Tab-Token', config.tabToken);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(`${apiBase}${path}`, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) {
    let detail;
    try { detail = await response.json(); } catch { /* HTML login/error responses are not shown as markup. */ }
    let message = detail?.error || detail?.message || `Request failed (${response.status}).`;
    if (response.status === 401 || response.status === 403) message = 'Your private session or tab verification expired. Reload /shygarma to verify again. Any unsaved text is still in this tab.';
    throw new ApiError(message, response.status);
  }
  return response;
}

async function json(path, options = {}) {
  const response = await request(path, options);
  return response.json();
}

const postFrom = (result) => result.post || result;
function message(text = '') {
  $('#editor-message').textContent = text;
  $('#editor-message').hidden = !text;
}
function status(text) { $('#save-status').textContent = text; }
function report(error) { message(error.message || 'Something went wrong. Try again.'); }
function savedStatus(post = current) {
  if (post?.archived_at) return 'Saved privately · archived article';
  return post?.published_at ? 'Changes saved privately · Update to publish' : 'All changes saved';
}

function mutationBusy(value) {
  busy = value;
  editor.setEditable(!value, false);
  $('#post-title').disabled = value;
  $('#post-picker').disabled = value;
  $('#new-post').disabled = value;
  updateMeta();
}

async function mediaBlob(id) {
  if (!mediaBlobs.has(id)) {
    const promise = request(`/media/${encodeURIComponent(id)}`).then((response) => response.blob()).catch((error) => { mediaBlobs.delete(id); throw error; });
    mediaBlobs.set(id, promise);
  }
  return mediaBlobs.get(id);
}

async function mediaUrl(id) {
  if (!mediaCache.has(id)) {
    const promise = mediaBlob(id).then((blob) => {
      const url = URL.createObjectURL(blob);
      objectUrls.add(url);
      return url;
    }).catch((error) => { mediaCache.delete(id); throw error; });
    mediaCache.set(id, promise);
  }
  return mediaCache.get(id);
}

async function mediaDataUrl(id) {
  const blob = await mediaBlob(id);
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('The preview image could not be read.'));
    reader.readAsDataURL(blob);
  });
}

const Spoiler = Mark.create({
  name: 'spoiler',
  parseHTML: () => [{ tag: 'span[data-spoiler]' }],
  renderHTML: ({ HTMLAttributes }) => ['span', mergeAttributes(HTMLAttributes, { 'data-spoiler': '', class: 'editor-spoiler' }), 0],
  addCommands() { return { toggleSpoiler: () => ({ commands }) => commands.toggleMark(this.name) }; },
});

const ImageBlock = Node.create({
  name: 'image', group: 'block', atom: true, draggable: true, selectable: true,
  addAttributes() {
    return {
      uploadId: { default: null, parseHTML: (element) => element.getAttribute('data-upload-id') },
      alt: { default: '', parseHTML: (element) => element.querySelector('img')?.alt || '' },
      caption: { default: '', parseHTML: (element) => element.querySelector('figcaption')?.textContent || '' },
    };
  },
  parseHTML: () => [{ tag: 'figure[data-upload-id]' }],
  renderHTML: ({ node }) => ['figure', { 'data-upload-id': node.attrs.uploadId }, ['img', { alt: node.attrs.alt }], ['figcaption', {}, node.attrs.caption || '']],
  addNodeView() {
    return ({ node }) => {
      const dom = document.createElement('figure');
      dom.className = 'editor-figure';
      dom.setAttribute('contenteditable', 'false');
      const img = document.createElement('img');
      img.draggable = false;
      const placeholder = document.createElement('div');
      placeholder.className = 'image-placeholder';
      placeholder.textContent = 'Loading private image…';
      const caption = document.createElement('figcaption');
      dom.append(placeholder, img, caption);
      let latestId;
      let destroyed = false;
      function draw(next) {
        img.alt = next.attrs.alt || '';
        caption.textContent = next.attrs.caption || '';
        caption.hidden = !next.attrs.caption;
        if (next.attrs.uploadId !== latestId) {
          latestId = next.attrs.uploadId;
          img.hidden = true;
          placeholder.hidden = false;
          const requestedId = latestId;
          mediaUrl(latestId).then((url) => {
            if (destroyed || latestId !== requestedId) return;
            img.src = url;
            img.hidden = false;
            placeholder.hidden = true;
          }).catch(() => { if (!destroyed) placeholder.textContent = 'Image unavailable. Reopen after verifying this tab.'; });
        }
      }
      draw(node);
      return {
        dom,
        update(next) { if (next.type.name !== 'image') return false; draw(next); return true; },
        selectNode() { dom.classList.add('ProseMirror-selectednode'); },
        deselectNode() { dom.classList.remove('ProseMirror-selectednode'); },
        destroy() { destroyed = true; },
      };
    };
  },
});

let editor;
editor = new Editor({
  element: $('#article-editor'),
  extensions: [StarterKit.configure({ heading: { levels: [2, 3] }, horizontalRule: false, link: { openOnClick: false, autolink: false, linkOnPaste: false } }), Spoiler, ImageBlock],
  content: emptyBody(),
  editorProps: {
    attributes: { 'aria-label': 'Article body', role: 'textbox', 'aria-multiline': 'true', spellcheck: 'true' },
    handlePaste(view, event) {
      const files = [...(event.clipboardData?.files || [])].filter((file) => file.type.startsWith('image/'));
      if (!files.length) return false;
      event.preventDefault();
      uploadImages(files).catch(report);
      return true;
    },
    handleDrop(view, event) {
      const files = [...(event.dataTransfer?.files || [])].filter((file) => file.type.startsWith('image/'));
      if (!files.length) return false;
      event.preventDefault();
      const position = view.posAtCoords({ left: event.clientX, top: event.clientY });
      if (position) editor.commands.setTextSelection(position.pos);
      uploadImages(files).catch(report);
      return true;
    },
  },
  onUpdate() { if (!loading) changed(); },
  onSelectionUpdate() { syncToolbar(); },
  onTransaction() { syncToolbar(); },
});

function syncToolbar() {
  if (!editor) return;
  document.querySelectorAll('[data-command]').forEach((button) => {
    const command = button.dataset.command;
    if (command === 'undo' || command === 'redo') {
      button.disabled = command === 'undo' ? !editor.can().undo() : !editor.can().redo();
    } else {
      button.setAttribute('aria-pressed', String(editor.isActive(command)));
    }
  });
  $('#block-format').value = editor.isActive('heading', { level: 2 }) ? '2' : editor.isActive('heading', { level: 3 }) ? '3' : 'paragraph';
  const selection = editor.state.selection;
  const image = selection instanceof NodeSelection && selection.node.type.name === 'image' ? selection.node : null;
  $('#image-details').hidden = !image;
  if (image) {
    if (document.activeElement !== $('#image-alt')) $('#image-alt').value = image.attrs.alt || '';
    if (document.activeElement !== $('#image-caption')) $('#image-caption').value = image.attrs.caption || '';
  }
}

function changed() {
  if (!current || loading) return;
  revision += 1;
  status(conflicted ? 'Conflict: your unsaved text is still here.' : current.published_at ? 'Unsaved private changes…' : 'Unsaved changes…');
  clearTimeout(saveTimer);
  if (!conflicted) saveTimer = setTimeout(() => save().catch(report), 700);
}

function refreshPicker() {
  const picker = $('#post-picker');
  picker.replaceChildren(new Option('Choose an article', ''));
  for (const [label, matches] of [
    ['Drafts', (post) => !post.published_at && !post.archived_at],
    ['Published', (post) => post.published_at && !post.archived_at],
    ['Archived · private', (post) => post.archived_at],
  ]) {
    const group = document.createElement('optgroup');
    group.label = label;
    for (const article of articles.filter(matches)) group.append(new Option(article.title || 'Untitled draft', String(article.id)));
    if (group.childElementCount) picker.append(group);
  }
  picker.value = current ? String(current.id) : '';
}
function rememberPost(post) {
  const index = articles.findIndex((item) => String(item.id) === String(post.id));
  if (index === -1) articles.unshift(post); else articles[index] = { ...articles[index], ...post };
  refreshPicker();
}
function displayPost(post) {
  resetNotificationTracking(true);
  loading = true;
  current = post;
  history.replaceState({ ...(history.state || {}), editorPostId: post.id }, '');
  revision = savedRevision = 0;
  conflicted = false;
  $('#reload-post').hidden = true;
  $('#retry-save').hidden = true;
  $('#empty-editor').hidden = true;
  $('#editing-panel').hidden = false;
  $('#post-title').value = post.title || '';
  editor.commands.setContent(post.body || emptyBody(), { emitUpdate: false });
  // A reopened article starts its own undo history. Undo must never bring text
  // from the article that was previously open into this one.
  editor.view.updateState(EditorState.create({ schema: editor.schema, doc: editor.state.doc, plugins: editor.state.plugins }));
  editor.setEditable(true, false);
  updateMeta();
  status(savedStatus(post));
  rememberPost(post);
  message();
  loading = false;
  syncToolbar();
  refreshNotification(post.id);
}
function updateMeta() {
  if (!current) return;
  $('#post-kind').textContent = current.archived_at ? 'Archived article · private working copy' : current.published_at ? 'Published article · private working copy' : 'Private draft';
  $('#publish-post').textContent = current.published_at ? 'Update' : 'Publish';
  const url = $('#post-url');
  url.replaceChildren();
  if (current.published_at && !current.archived_at && current.slug) {
    const link = document.createElement('a');
    link.href = `/posts/${encodeURIComponent(current.slug)}`;
    link.textContent = 'View published article';
    link.target = '_blank';
    link.rel = 'noopener';
    url.append(link);
  }
  $('#publish-post').disabled = conflicted || busy || Boolean(current.archived_at);
  $('#archive-post').hidden = !current.published_at || Boolean(current.archived_at);
  $('#restore-post').hidden = !current.archived_at;
  for (const selector of ['#archive-post', '#restore-post', '#delete-post']) $(selector).disabled = conflicted || busy;
  $('#preview-post').disabled = busy;
  $('#retry-notification').disabled = busy;
}

async function save() {
  clearTimeout(saveTimer);
  if (conflicted) throw new ApiError('This article changed in another tab. Reload its saved version before continuing.', 409);
  if (savePromise) { await savePromise; if (revision !== savedRevision) return save(); return; }
  if (!current || savedRevision === revision) return;
  const target = current.id;
  const snapshotRevision = revision;
  const payload = { title: $('#post-title').value.trim() || 'Untitled draft', body: editor.getJSON(), version: current.version };
  status('Saving…');
  savePromise = json(`/posts/${target}`, { method: 'PUT', body: JSON.stringify(payload) }).then((result) => {
    const post = postFrom(result);
    if (current?.id !== target) return;
    current = { ...current, ...post };
    savedRevision = snapshotRevision;
    rememberPost(current);
    $('#retry-save').hidden = true;
    status(revision === savedRevision ? savedStatus() : 'Saving newer changes…');
  }).catch((error) => {
    if (error.status === 409) {
      conflicted = true;
      $('#reload-post').hidden = false;
      $('#publish-post').disabled = true;
      status('Another tab saved a newer version. Your text is still here.');
      error.message = 'A newer saved version exists. Copy any text you want to keep, then use Reload saved version. Your unsaved text has not overwritten it.';
    } else {
      $('#retry-save').hidden = false;
      status('Not saved · your changes are still in this tab');
    }
    throw error;
  }).finally(() => { savePromise = null; });
  await savePromise;
  if (revision !== savedRevision) return save();
}

async function switchPost(id) {
  if (busy) return;
  resetNotificationTracking();
  const previous = current?.id;
  busy = true;
  $('#post-picker').disabled = true;
  try {
    await save();
    const post = postFrom(await json(`/posts/${encodeURIComponent(id)}`));
    displayPost(post);
  } catch (error) { $('#post-picker').value = previous ? String(previous) : ''; report(error); }
  finally { busy = false; $('#post-picker').disabled = false; if (current) updateMeta(); }
}

async function newPost() {
  if (busy) return;
  resetNotificationTracking();
  busy = true;
  $('#new-post').disabled = true;
  try {
    await save();
    const post = postFrom(await json('/posts', { method: 'POST', body: JSON.stringify({ title: 'Untitled draft', body: emptyBody() }) }));
    displayPost(post);
    $('#post-title').focus();
  } catch (error) { report(error); }
  finally { busy = false; $('#new-post').disabled = false; if (current) updateMeta(); }
}

async function uploadImages(files) {
  if (!current || busy) return;
  busy = true;
  $('#insert-image').disabled = true;
  $('#post-picker').disabled = true;
  $('#new-post').disabled = true;
  updateMeta();
  message();
  try {
    for (const file of files) {
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('Choose a PNG, JPEG, or WebP image.');
      if (file.size > (config.maxImageBytes || 10 * 1024 * 1024)) throw new Error('That image is too large. Choose an image smaller than 10 MB.');
      status('Uploading image…');
      const data = new FormData();
      data.append('file', file, file.name || 'clipboard-image.png');
      const result = await json('/uploads', { method: 'POST', body: data });
      const id = result.id || result.uploadId || result.upload?.id;
      if (!id) throw new Error('The server did not return an image ID.');
      editor.chain().focus().insertContent({ type: 'image', attrs: { uploadId: id, alt: '', caption: '' } }).run();
    }
    await save();
  } finally {
    busy = false;
    $('#insert-image').disabled = false;
    $('#post-picker').disabled = false;
    $('#new-post').disabled = false;
    updateMeta();
  }
}

function moveImage(direction) {
  const selection = editor.state.selection;
  if (!(selection instanceof NodeSelection) || selection.node.type.name !== 'image') return;
  const doc = editor.state.doc;
  const index = selection.$from.index(0);
  const targetIndex = index + direction;
  if (targetIndex < 0 || targetIndex >= doc.childCount) return;
  const image = selection.node;
  const adjacent = doc.child(targetIndex);
  const position = direction < 0 ? selection.from - adjacent.nodeSize : selection.from + adjacent.nodeSize;
  let transaction = editor.state.tr.delete(selection.from, selection.to).insert(position, image);
  transaction = transaction.setSelection(NodeSelection.create(transaction.doc, position));
  editor.view.dispatch(transaction);
  editor.view.focus();
}

async function preview() {
  if (!current || busy) return;
  try {
    await save();
    status('Preparing preview…');
    const response = await request(`/posts/${current.id}/preview`, { method: 'POST', body: JSON.stringify({ version: current.version }) });
    const data = response.headers.get('Content-Type')?.includes('application/json') ? await response.json() : { html: await response.text() };
    const documentPreview = new DOMParser().parseFromString(data.html || '', 'text/html');
    // Draft media is loaded with this tab's capability, then placed in the
    // sandboxed preview as data URLs. The opaque-origin iframe cannot rely on
    // the parent document's blob URL origin. No proof is exposed in a URL.
    await Promise.all([...documentPreview.querySelectorAll('img')].map(async (img) => {
      const match = (img.getAttribute('src') || '').match(/\/(?:media|api\/media)\/([a-f0-9]{32})\/?$/);
      if (match) img.src = await mediaDataUrl(match[1]);
    }));
    $('#preview-frame').srcdoc = '<!doctype html>' + documentPreview.documentElement.outerHTML;
    $('#preview-dialog').showModal();
    status(savedStatus());
  } catch (error) {
    if (error.status === 409) {
      conflicted = true;
      $('#reload-post').hidden = false;
      status('Another tab saved a newer version. Reload before previewing.');
      updateMeta();
    }
    report(error);
  }
}

async function publish() {
  if (!current || busy || conflicted || current.archived_at) return;
  resetNotificationTracking();
  $('#publish-dialog').close();
  mutationBusy(true);
  try {
    await save();
    const wasPublished = Boolean(current.published_at);
    const result = await json(`/posts/${current.id}/publish`, { method: 'POST', body: JSON.stringify({ version: current.version, notify_subscribers: !wasPublished && $('#notify-subscribers').checked }) });
    current = { ...current, ...postFrom(result) };
    savedRevision = revision;
    rememberPost(current);
    updateMeta();
    status(wasPublished ? 'Published article updated' : 'Published · now on Main');
    message(wasPublished ? 'Your changes are public. The original publication date and permanent URL are preserved.' : 'Your article is public on Main and at its permanent URL.');
    if (result.notification) displayNotification(result.notification); else refreshNotification(current.id);
  } catch (error) {
    if (error.status === 409) {
      conflicted = true;
      $('#reload-post').hidden = false;
      status('Another tab saved a newer version. Reload before publishing.');
    }
    report(error);
  }
  finally { mutationBusy(false); }
}

function notificationRefreshNote(text = '') {
  let note = $('#notification-refresh-note');
  if (!note) {
    note = document.createElement('p');
    note.id = 'notification-refresh-note';
    note.className = 'editor-hint';
    $('#notification-status').after(note);
  }
  note.textContent = text;
  note.hidden = !text;
}

function resetNotificationTracking(hide = false) {
  notificationGeneration += 1;
  clearTimeout(notificationTimer);
  notificationTimer = null;
  notificationRequest?.controller.abort();
  notificationRequest = null;
  notificationPollsLeft = 36;
  lastNotificationStatus = null;
  notificationRefreshNote();
  if (hide) $('#notification-panel').hidden = true;
}

function scheduleNotificationRefresh() {
  clearTimeout(notificationTimer);
  notificationTimer = null;
  if (!current?.published_at || current.archived_at || !['pending', 'queued', 'retry', 'sending'].includes(lastNotificationStatus)) return;
  if (notificationPollsLeft <= 0) {
    notificationRefreshNote('Automatic status checks paused after 36 checks. Reopen this article to refresh its email status.');
    return;
  }
  const id = current.id;
  const generation = notificationGeneration;
  notificationTimer = setTimeout(() => {
    notificationTimer = null;
    if (generation !== notificationGeneration || current?.id !== id || current.archived_at) return;
    notificationPollsLeft -= 1;
    refreshNotification(id);
  }, 5000);
}

function displayNotification(notification) {
  const panel = $('#notification-panel');
  if (!notification || !notification.status) {
    lastNotificationStatus = null;
    clearTimeout(notificationTimer);
    panel.hidden = true;
    return;
  }
  lastNotificationStatus = notification.status;
  const descriptions = {
    pending: 'Email notification is queued. Publishing is complete; delivery can be retried safely.',
    queued: 'Email notification is queued. Publishing is complete; delivery will run in the background.',
    retry: 'The article is public. Email delivery has not been confirmed; a safe retry is queued.',
    sending: 'Email notification is being processed.',
    failed: 'The article is public, but its email notification has not been sent. Retry after the email service recovers.',
    needs_review: 'Email delivery needs review before another attempt. Check the provider status to avoid a duplicate send.',
    sent: 'Email notification accepted by the email service. Updating or restoring this article will not send another notice.',
    skipped: 'Email notification skipped because this article is no longer public.',
    disabled: 'No email notification was requested for this article.',
    not_requested: 'No email notification was requested on first publication. Update and Restore will not send one.',
    no_confirmed_recipients: 'No email was sent because there were no confirmed subscribers at first publication.',
    configuration_required: 'The article is public. Email delivery needs configuration before its queued notification can be sent.',
    test: 'Email notification is limited to the configured test address.',
    test_sent: 'Test email accepted by the email service for the configured test address. No subscriber broadcast was sent.',
  };
  const text = descriptions[notification.status] || `Email notification: ${notification.status}.`;
  const countLabels = { queued: 'queued', sending: 'sending', sent: 'accepted by service', retry: 'awaiting retry', failed: 'failed', needs_review: 'need review', skipped: 'skipped' };
  const counts = Object.entries(notification.counts || {}).filter(([, count]) => Number.isInteger(count) && count > 0)
    .map(([state, count]) => `${count} ${countLabels[state] || state}`).join(', ');
  $('#notification-status').textContent = `${text}${counts ? ` Delivery records: ${counts}.` : ''}${notification.error ? ` ${notification.error}` : ''}`;
  $('#retry-notification').hidden = !['pending', 'queued', 'retry', 'failed', 'configuration_required'].includes(notification.status) || Boolean(current?.archived_at);
  $('#retry-notification').disabled = busy;
  panel.hidden = false;
  notificationRefreshNote();
  scheduleNotificationRefresh();
}

async function refreshNotification(id) {
  if (!current?.published_at || current.id !== id) return;
  if (notificationRequest?.generation === notificationGeneration) return notificationRequest.promise;
  const generation = notificationGeneration;
  const token = { generation, controller: new AbortController(), promise: null };
  notificationRequest = token;
  token.promise = (async () => {
    try {
      const result = await json(`/posts/${id}/notification`, { signal: token.controller.signal });
      if (generation === notificationGeneration && current?.id === id) displayNotification(result.notification || result);
    } catch (error) {
      if (error.name === 'AbortError' || generation !== notificationGeneration || current?.id !== id) return;
      const panel = $('#notification-panel');
      if (panel.hidden) {
        $('#notification-status').textContent = 'Email notification status is currently unavailable.';
        $('#retry-notification').hidden = true;
        panel.hidden = false;
      }
      // A transient status read must not replace the last durable send status.
      notificationRefreshNote(`Status refresh failed: ${error.message}`);
      if (![401, 403].includes(error.status)) scheduleNotificationRefresh();
    } finally {
      if (notificationRequest === token) notificationRequest = null;
    }
  })();
  return token.promise;
}

function displayCleanup(cleanup) {
  if (!cleanup) return;
  const pending = Number(cleanup.pending || 0);
  const failed = Number(cleanup.failed || 0);
  const removed = Number(cleanup.removed || 0);
  const errors = Array.isArray(cleanup.errors) ? cleanup.errors.map((item) => typeof item === 'string' ? item : item?.error).filter((item) => typeof item === 'string') : [];
  $('#cleanup-panel').hidden = false;
  $('#cleanup-status').textContent = `File cleanup: ${removed} file${removed === 1 ? '' : 's'} removed; ${pending} awaiting retry.${failed ? ` ${failed} cleanup attempt${failed === 1 ? '' : 's'} failed.` : ''}${errors.length ? ` ${errors.join(' ')}` : ''}`;
  $('#retry-cleanup').hidden = pending === 0 && failed === 0;
}

function managementError(error) {
  if (error.status === 409) {
    conflicted = true;
    $('#reload-post').hidden = false;
    status('Another tab changed this article. Reload its saved version before continuing.');
  }
  report(error);
}

function showStateDialog(action) {
  if (!current || busy || conflicted) return;
  if (action === 'archive' && (!current.published_at || current.archived_at)) return;
  if (action === 'restore' && !current.archived_at) return;
  pendingStateAction = { action, id: current.id };
  const restoring = action === 'restore';
  $('#state-confirm-title').textContent = restoring ? 'Restore to published?' : 'Archive this article?';
  $('#state-confirm-copy').textContent = restoring
    ? `“${$('#post-title').value.trim() || 'Untitled draft'}” will return at its original permanent URL and publication date. Its last published version will become public; private changes stay private. No email will be sent.`
    : `“${$('#post-title').value.trim() || 'Untitled draft'}” will disappear from public pages and its permanent URL. Its article, private working copy, date, and images will remain in storage.`;
  $('#confirm-state').textContent = restoring ? 'Restore to published' : 'Archive article';
  $('#post-state-dialog').showModal();
}

async function changePostState() {
  if (!current || busy || conflicted || pendingStateAction?.id !== current.id) return;
  resetNotificationTracking();
  const { action } = pendingStateAction;
  $('#post-state-dialog').close();
  mutationBusy(true);
  try {
    await save();
    const result = await json(`/posts/${current.id}/${action}`, { method: 'POST', body: JSON.stringify({ version: current.version }) });
    current = { ...current, ...postFrom(result) };
    rememberPost(current);
    status(savedStatus());
    message(action === 'archive'
      ? 'Article archived. It and its private working copy are available in Archived; its public URL now returns 404.'
      : 'Article restored at its original URL and publication date. Private changes remain private until Update. No new-post email was sent.');
    if (result.notification) displayNotification(result.notification); else refreshNotification(current.id);
    displayCleanup(result.cleanup);
  } catch (error) { managementError(error); }
  finally { pendingStateAction = null; mutationBusy(false); }
}

function clearPost() {
  resetNotificationTracking(true);
  loading = true;
  current = null;
  revision = savedRevision = 0;
  conflicted = false;
  const nextHistory = { ...(history.state || {}) };
  delete nextHistory.editorPostId;
  history.replaceState(nextHistory, '');
  editor.commands.setContent(emptyBody(), { emitUpdate: false });
  $('#post-title').value = '';
  $('#editing-panel').hidden = true;
  $('#empty-editor').hidden = false;
  $('#notification-panel').hidden = true;
  refreshPicker();
  loading = false;
}

async function deletePost(event) {
  event.preventDefault();
  if (!current || busy || conflicted || $('#delete-confirmation').value !== 'DELETE') return;
  $('#delete-dialog').close();
  mutationBusy(true);
  try {
    await save();
    const id = current.id;
    const result = await json(`/posts/${id}`, { method: 'DELETE', body: JSON.stringify({ confirmation: 'DELETE', version: current.version }) });
    articles = articles.filter((post) => post.id !== id);
    clearPost();
    for (const url of objectUrls) URL.revokeObjectURL(url);
    objectUrls.clear();
    mediaCache.clear();
    mediaBlobs.clear();
    message('Article permanently deleted. Its former public URL returns 404. Any pending file cleanup is shown below.');
    if (result.deletion_ledger?.saved === false) message('Article permanently deleted. Its former public URL returns 404. The separate deletion ledger could not be saved. Make a verified backup and keep the newest deletion history outside the data directory before relying on recovery. File cleanup status is shown below.');
    displayCleanup(result.cleanup);
  } catch (error) { managementError(error); }
  finally { mutationBusy(false); }
}

document.querySelectorAll('[data-command]').forEach((button) => {
  button.addEventListener('mousedown', (event) => event.preventDefault());
  button.addEventListener('click', () => {
    if (busy || !current) return;
    const command = button.dataset.command;
    if (command === 'link') {
      $('#link-url').value = editor.getAttributes('link').href || '';
      $('#link-error').textContent = '';
      $('#link-dialog').showModal();
      $('#link-url').focus();
      return;
    }
    const methods = { bold: 'toggleBold', italic: 'toggleItalic', underline: 'toggleUnderline', strike: 'toggleStrike', bulletList: 'toggleBulletList', orderedList: 'toggleOrderedList', blockquote: 'toggleBlockquote', code: 'toggleCode', codeBlock: 'toggleCodeBlock', spoiler: 'toggleSpoiler', undo: 'undo', redo: 'redo' };
    editor.chain().focus()[methods[command]]().run();
  });
});
$('#block-format').addEventListener('change', (event) => {
  if (busy || !current) return;
  const chain = editor.chain().focus();
  if (event.target.value === 'paragraph') chain.setParagraph().run(); else chain.toggleHeading({ level: Number(event.target.value) }).run();
});
$('#apply-link').addEventListener('click', () => {
  if (busy || !current) return;
  const href = $('#link-url').value.trim();
  if (!href || !/^(https?:\/\/[^\s\\]+|\/(?!\/)[^\s\\]*)$/i.test(href) || /[\u0000-\u001f\u007f]/.test(href)) {
    $('#link-error').textContent = 'Use a full http:// or https:// address, or a site path starting with one /. ';
    return;
  }
  editor.chain().focus().extendMarkRange('link').setLink({ href }).run();
  $('#link-dialog').close();
});
$('#remove-link').addEventListener('click', () => { if (busy || !current) return; editor.chain().focus().extendMarkRange('link').unsetLink().run(); $('#link-dialog').close(); });
$('#post-title').addEventListener('input', changed);
$('#post-picker').addEventListener('change', (event) => { if (event.target.value) switchPost(event.target.value); });
$('#new-post').addEventListener('click', newPost);
$('#retry-save').addEventListener('click', () => save().then(() => message()).catch(report));
$('#reload-post').addEventListener('click', async () => {
  if (!current) return;
  try { displayPost(postFrom(await json(`/posts/${current.id}`))); } catch (error) { report(error); }
});
$('#insert-image').addEventListener('click', () => $('#image-picker').click());
$('#image-picker').addEventListener('change', (event) => { uploadImages([...event.target.files]).catch(report); event.target.value = ''; });
$('#image-alt').addEventListener('input', (event) => { if (!busy) editor.commands.updateAttributes('image', { alt: event.target.value }); });
$('#image-caption').addEventListener('input', (event) => { if (!busy) editor.commands.updateAttributes('image', { caption: event.target.value }); });
$('#image-up').addEventListener('click', () => { if (!busy) moveImage(-1); });
$('#image-down').addEventListener('click', () => { if (!busy) moveImage(1); });
$('#image-remove').addEventListener('click', () => { if (!busy) editor.chain().focus().deleteSelection().run(); });
$('#preview-post').addEventListener('click', preview);
$('#close-preview').addEventListener('click', () => { $('#preview-dialog').close(); $('#preview-frame').srcdoc = ''; });
$('#publish-post').addEventListener('click', () => {
  if (!current || current.archived_at || busy || conflicted) return;
  if (!$('#post-title').value.trim()) { message('Give the article a title before publishing.'); $('#post-title').focus(); return; }
  const published = Boolean(current.published_at);
  $('#publish-confirm-title').textContent = published ? 'Update this published article?' : 'Publish this article?';
  $('#publish-confirm-copy').textContent = published ? 'Your saved changes will become public. Its original date and URL will stay the same.' : 'It will appear on Main and have a public permanent URL.';
  $('#confirm-publish').textContent = published ? 'Update article' : 'Publish article';
  $('#notify-choice').hidden = published;
  $('#notify-choice-hint').hidden = published;
  $('#notify-subscribers').checked = false;
  $('#publish-dialog').showModal();
});
$('#cancel-publish').addEventListener('click', () => $('#publish-dialog').close());
$('#confirm-publish').addEventListener('click', publish);
$('#archive-post').addEventListener('click', () => showStateDialog('archive'));
$('#restore-post').addEventListener('click', () => showStateDialog('restore'));
$('#cancel-state').addEventListener('click', () => { pendingStateAction = null; $('#post-state-dialog').close(); });
$('#confirm-state').addEventListener('click', changePostState);
$('#delete-post').addEventListener('click', () => {
  if (!current || busy || conflicted) return;
  $('#delete-post-title').textContent = $('#post-title').value.trim() || 'Untitled draft';
  $('#delete-confirmation').value = '';
  $('#confirm-delete').disabled = true;
  $('#delete-dialog').showModal();
  $('#delete-confirmation').focus();
});
$('#delete-confirmation').addEventListener('input', () => { $('#confirm-delete').disabled = $('#delete-confirmation').value !== 'DELETE'; });
$('#cancel-delete').addEventListener('click', () => $('#delete-dialog').close());
$('#delete-form').addEventListener('submit', deletePost);
$('#retry-cleanup').addEventListener('click', async () => {
  const button = $('#retry-cleanup');
  button.disabled = true;
  try {
    const result = await json('/media-cleanup', { method: 'POST', body: JSON.stringify({}) });
    displayCleanup(result.cleanup || result);
  } catch (error) { report(error); }
  finally { button.disabled = false; }
});
$('#retry-notification').addEventListener('click', async () => {
  if (!current || busy || current.archived_at) return;
  const id = current.id;
  resetNotificationTracking();
  const generation = notificationGeneration;
  $('#retry-notification').disabled = true;
  try {
    const result = await json(`/posts/${id}/notification/retry`, { method: 'POST', body: JSON.stringify({}) });
    if (current?.id === id && generation === notificationGeneration) displayNotification(result.notification || result);
  } catch (error) { report(error); }
  finally { $('#retry-notification').disabled = false; }
});
window.addEventListener('beforeunload', (event) => {
  if (revision !== savedRevision) { event.preventDefault(); event.returnValue = ''; }
});
window.addEventListener('pagehide', () => { resetNotificationTracking(); for (const url of objectUrls) URL.revokeObjectURL(url); });
$('#logout-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await save();
    const response = await fetch('/shygarma/logout', { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'X-CSRF-Token': config.csrf, 'X-Tab-Token': config.tabToken } });
    if (!response.ok) throw new Error('Sign out failed. Please try again.');
    window.name = '';
    window.location.assign('/shygarma');
  } catch (error) { report(error); }
});

refreshPicker();
if (config.post) displayPost(config.post);
else if (history.state?.editorPostId) switchPost(history.state.editorPostId);
