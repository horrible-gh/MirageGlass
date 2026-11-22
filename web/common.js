'use strict';

const $ = id => document.getElementById(id);
function t(key, values = {}) {
  return Object.entries(values).reduce((text, [name, value]) => text.replaceAll('{' + name + '}', String(value)), window.MG_STRINGS[key] || key);
}
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
}
function localize(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(element => { element.textContent = t(element.dataset.i18n); });
  root.querySelectorAll('[data-i18n-label]').forEach(element => { element.setAttribute('aria-label', t(element.dataset.i18nLabel)); element.title = t(element.dataset.i18nLabel); });
  root.querySelectorAll('[data-i18n-placeholder]').forEach(element => { element.placeholder = t(element.dataset.i18nPlaceholder); });
}
function readJsonList(key) {
  try { const value = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(value) ? value.filter(item => typeof item === 'string') : []; }
  catch (_) { return []; }
}
function readHiddenPreference(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value === 'true' ? true : value === 'false' ? false : fallback;
  } catch (_) { return fallback; }
}
function saveHiddenPreference(key, hidden) {
  try { localStorage.setItem(key, String(hidden)); } catch (_) { /* Keep the current page preference. */ }
}
function formatTimestamp(iso) {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return '';
  const shifted = new Date(date.getTime() + 9 * 60 * 60 * 1000);
  return shifted.toISOString().replace('T', ' ').slice(0, 19);
}
function versionedThumbUrl(deck) {
  return deck.thumb_url ? deck.thumb_url + '?v=' + encodeURIComponent(deck.active_version ?? deck.version) : null;
}
function normalizeDeck(deck) { return {...deck,active_version:deck.version ?? deck.active_version}; }
const icons = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1"/>',
  star: '<path d="m12 3 2.8 6 6.7.7-5 4.7 1.4 6.6L12 17.7l-5.9 3.3 1.4-6.6-5-4.7L9.2 9 12 3Z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  screen: '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M8 21h8M12 18v3"/>',
  search: '<circle cx="10.5" cy="10.5" r="7"/><path d="m16 16 5 5"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  panelLeft: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  panelRight: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
  chevronLeft: '<path d="m14 6-6 6 6 6"/>',
  chevronRight: '<path d="m10 6 6 6-6 6"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  restore: '<path d="M9 3v6H3m12-6v6h6M9 21v-6H3m12 6v-6h6"/>',
  reset: '<path d="M4 5v5h5M5 9a8 8 0 1 1-.5 7"/>',
  compare: '<rect x="3" y="4" width="7" height="16" rx="1"/><rect x="14" y="4" width="7" height="16" rx="1"/>',
  plus: '<path d="M12 4v16M4 12h16"/>',
  refresh: '<path d="M20 4v6h-6M4 20v-6h6M5 8a8 8 0 0 1 13-3l2 5M4 14l2 5a8 8 0 0 0 13-3"/>'
};
function icon(name) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (icons[name] || icons.screen) + '</svg>'; }
document.querySelectorAll('[data-icon]').forEach(element => { element.innerHTML = icon(element.dataset.icon); });
localize();

let toastTimer;
function announceLibrary(message) {
  $('lib-live').textContent = message;
  $('toast').textContent = message;
  $('toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.remove('show'), 3200);
}
let sessionRefresh = null;
async function reestablishConsoleSession() {
  if (!sessionRefresh) sessionRefresh = fetch('/', {credentials:'same-origin', cache:'no-store'}).then(response => response.ok).catch(() => false).finally(() => { sessionRefresh = null; });
  return sessionRefresh;
}
async function apiRequest(url, options = {}, retried = false) {
  const response = await fetch(url, {credentials:'same-origin', cache:'no-store', ...options});
  if (response.ok) return response.status === 204 ? null : response.json();
  if (response.status === 401 && !retried && options.method && options.method !== 'GET' && await reestablishConsoleSession()) return apiRequest(url, options, true);
  let detail;
  try { detail = (await response.json()).detail; } catch (_) { /* A server error may not include JSON. */ }
  const error = new Error(typeof detail === 'string' ? detail : detail?.message || t('requestFailed', {status:response.status}));
  error.status = response.status;
  error.code = detail?.code;
  throw error;
}
function jsonRequest(method, body) { return {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}; }
function describeApiError(error) {
  if (error.status === 401 || error.status === 403) return t('unauthorized');
  if (error.status === 404) return t('notFound');
  return error.status ? error.message : t('networkError');
}
async function showFailureAlert(error, title = t('actionFailed')) { await showAlert({title, bodyHtml:'<p>' + esc(describeApiError(error)) + '</p>'}); }

const dialogEls = {overlay:$('dialog-overlay'), box:$('dialog-box'), title:$('dialog-title'), body:$('dialog-body'), cancelBtn:$('dialog-cancel'), confirmBtn:$('dialog-confirm'), closeBtn:$('dialog-x')};
let activeDialog = null;
function dialogTrapKeydown(event) {
  if (event.key !== 'Tab') return;
  const fields = Array.from(dialogEls.box.querySelectorAll('button,input,select,textarea,a[href]')).filter(element => !element.disabled && element.offsetParent !== null);
  if (!fields.length) return;
  if (event.shiftKey && document.activeElement === fields[0]) { event.preventDefault(); fields.at(-1).focus(); }
  else if (!event.shiftKey && document.activeElement === fields.at(-1)) { event.preventDefault(); fields[0].focus(); }
}
function closeDialog(result) {
  const pending = activeDialog;
  activeDialog = null;
  dialogEls.overlay.hidden = true;
  dialogEls.body.innerHTML = '';
  document.removeEventListener('keydown', dialogTrapKeydown, true);
  if (pending) { if (pending.opener?.isConnected) pending.opener.focus(); pending.resolve(result); }
}
function openGenericDialog(config) {
  closeMenu();
  if (activeDialog) closeDialog({action:'cancel'});
  return new Promise(resolve => {
    activeDialog = {resolve, opener:document.activeElement};
    dialogEls.title.textContent = config.title;
    dialogEls.body.innerHTML = config.bodyHtml;
    dialogEls.cancelBtn.hidden = config.showCancel === false;
    dialogEls.cancelBtn.textContent = config.cancelText || t('cancel');
    dialogEls.confirmBtn.textContent = config.confirmText || t('ok');
    dialogEls.confirmBtn.classList.toggle('destructive', Boolean(config.destructive));
    dialogEls.confirmBtn.disabled = false;
    dialogEls.overlay.hidden = false;
    const fields = Array.from(dialogEls.body.querySelectorAll('input,select,textarea'));
    const required = fields.filter(field => field.hasAttribute('data-dialog-required'));
    const update = () => { dialogEls.confirmBtn.disabled = required.some(field => field.type === 'file' ? !field.files?.length : !field.value.trim()); };
    fields.forEach(field => { field.addEventListener('input', update); field.addEventListener('change', update); });
    fields.filter(field => field.tagName === 'INPUT' && field.type !== 'file').forEach(field => field.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !dialogEls.confirmBtn.disabled) { event.preventDefault(); dialogEls.confirmBtn.click(); }
    }));
    update();
    document.addEventListener('keydown', dialogTrapKeydown, true);
    (config.focus === 'confirm' || config.showCancel === false ? dialogEls.confirmBtn : dialogEls.cancelBtn).focus();
  });
}
dialogEls.closeBtn.onclick = dialogEls.cancelBtn.onclick = () => closeDialog({action:'cancel'});
dialogEls.confirmBtn.onclick = () => {
  const values = {};
  dialogEls.body.querySelectorAll('[data-dialog-field]').forEach(field => { values[field.dataset.dialogField] = field.type === 'file' ? field.files?.[0] || null : field.value; });
  const input = dialogEls.body.querySelector('input,textarea');
  closeDialog({action:'confirm', value:input ? input.value.trim() : true, values});
};
async function promptName({title, label, initialValue = ''}) {
  const result = await openGenericDialog({title, bodyHtml:'<label class="dialog-field">' + esc(label) + '<input id="dialog-input" maxlength="120" data-dialog-required value="' + esc(initialValue) + '"></label>', confirmText:t('save')});
  return result.action === 'confirm' ? result.value : null;
}
async function confirmDestructive(config) { return (await openGenericDialog({...config, confirmText:config.confirmText || t('delete'), destructive:true})).action === 'confirm'; }
async function showAlert(config) { return openGenericDialog({...config, showCancel:false, focus:'confirm'}); }

let openMenus = [];
let menuOpener = null;
function closeMenu(restoreFocus = false) {
  openMenus.forEach(menu => menu.remove()); openMenus = [];
  document.removeEventListener('pointerdown', onOutsideMenuClick, true);
  document.removeEventListener('keydown', onMenuKeydown, true);
  if (restoreFocus && menuOpener?.isConnected) menuOpener.focus();
  menuOpener = null;
}
function onOutsideMenuClick(event) { if (!openMenus.some(menu => menu.contains(event.target))) closeMenu(); }
function onMenuKeydown(event) {
  if (event.key === 'Escape') { event.preventDefault(); closeMenu(true); return; }
  const menu = document.activeElement?.closest('.lib-menu');
  if (!menu) return;
  const items = Array.from(menu.querySelectorAll('button:not(:disabled)'));
  if (!items.length) return;
  const index = items.indexOf(document.activeElement);
  const next = event.key === 'ArrowDown' ? (index + 1) % items.length : event.key === 'ArrowUp' ? (index + items.length - 1) % items.length : event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : null;
  if (next !== null) { event.preventDefault(); items[next].focus(); }
}
function openMenu(anchor, items) {
  closeMenu(); menuOpener = anchor;
  const menu = document.createElement('div'); menu.className = 'lib-menu'; menu.setAttribute('role','menu');
  items.forEach(item => {
    if (item.separator) { const line = document.createElement('div'); line.className = 'lib-menu-sep'; line.setAttribute('role','separator'); menu.append(line); return; }
    const button = document.createElement('button'); button.type = 'button'; button.className = 'lib-menu-item' + (item.destructive ? ' destructive' : '');
    button.setAttribute('role','menuitem'); button.textContent = item.label; button.disabled = Boolean(item.disabled); if (item.action) button.dataset.action = item.action;
    button.onclick = () => { closeMenu(true); Promise.resolve(item.onClick()).catch(error => showFailureAlert(error)); };
    menu.append(button);
  });
  document.body.append(menu);
  const rect = anchor.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(rect.bottom + 4, innerHeight - menu.offsetHeight - 8)) + 'px';
  openMenus.push(menu); menu.querySelector('button:not(:disabled)')?.focus();
  document.addEventListener('pointerdown', onOutsideMenuClick, true); document.addEventListener('keydown', onMenuKeydown, true);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); announceLibrary(t('copied')); }
  catch (_) {
    const result = showAlert({title:t('copyLink'), bodyHtml:'<p>' + esc(t('copyManually')) + '</p><input class="copy-fallback" aria-label="URL" value="' + esc(text) + '" readonly>'});
    dialogEls.body.querySelector('input').select(); await result;
  }
}
function copyDeckLink(deck) { return copyText(location.origin + deck.viewer_url); }

let uploadInFlight = false;
function uploadIdempotencyKey(kind) { return 'console-' + kind + '-' + (globalThis.crypto?.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2)); }
async function promptZipUpload({title, includeName = false}) {
  const pending = openGenericDialog({title, confirmText:t('upload'), bodyHtml:(includeName ? '<label class="dialog-field">' + esc(t('deckName')) + '<input maxlength="120" data-dialog-field="name" data-dialog-required></label>' : '')
    + '<label class="upload-dropzone" tabindex="0"><input class="upload-file-input" type="file" accept=".zip,application/zip" data-dialog-field="file" data-dialog-required><strong>' + esc(t('dropZip')) + '</strong><span>' + esc(t('chooseFile')) + '</span><small class="upload-file-name">' + esc(t('zipOnly')) + '</small></label>'});
  const zone = dialogEls.body.querySelector('.upload-dropzone'), input = zone.querySelector('input');
  input.onchange = () => { zone.querySelector('small').textContent = input.files?.[0]?.name || t('zipOnly'); };
  zone.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input.click(); } };
  ['dragenter','dragover'].forEach(type => zone.addEventListener(type, event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; zone.classList.add('drag-over'); }));
  ['dragleave','drop'].forEach(type => zone.addEventListener(type, event => { event.preventDefault(); zone.classList.remove('drag-over'); }));
  zone.addEventListener('drop', event => { if (!event.dataTransfer.files.length) return; const transfer = new DataTransfer(); transfer.items.add(event.dataTransfer.files[0]); input.files = transfer.files; input.dispatchEvent(new Event('change',{bubbles:true})); });
  const result = await pending;
  return result.action === 'confirm' ? result.values : null;
}
async function uploadNewDeck() {
  if (uploadInFlight) return;
  const selection = await promptZipUpload({title:t('uploadDeck'), includeName:true});
  if (!selection?.file) return;
  uploadInFlight = true;
  const form = new FormData(); form.append('file',selection.file,selection.file.name); form.append('name',selection.name.trim()); form.append('idempotency_key',uploadIdempotencyKey('deck'));
  try { await apiRequest('/api/v1/decks',{method:'POST',body:form}); await loadLibrary(); announceLibrary(t('uploaded')); }
  catch (error) { await loadLibrary(); await showFailureAlert(error); }
  finally { uploadInFlight = false; }
}
async function uploadNewVersion() {
  if (uploadInFlight || !currentId) return;
  const id = currentId;
  const selection = await promptZipUpload({title:t('uploadVersion')});
  if (!selection?.file || id !== currentId) return;
  uploadInFlight = true;
  const form = new FormData(); form.append('file',selection.file,selection.file.name); form.append('idempotency_key',uploadIdempotencyKey('version'));
  try { await apiRequest('/api/v1/decks/' + encodeURIComponent(id) + '/versions',{method:'POST',body:form}); await refreshViewerAfterUpload(id); announceLibrary(t('uploaded')); }
  catch (error) { await showFailureAlert(error); }
  finally { uploadInFlight = false; }
}
