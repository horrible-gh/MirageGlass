'use strict';

let decks = [];
let decksLoaded = false;
let decksLoadFailed = false;
let currentId = null;
let railScopeIds = null;
let screens = [];
let currentScreenKey = null;
let scope = 'deck';
let versionData = null;
let mode = 'single';
let versionA = null;
let versionB = null;
let viewingVersion = null;
let versionLoadGeneration = 0;
let frameGeneration = 0;
let viewerLoadGeneration = 0;
let historyVisible = !readHiddenPreference('mg.history.hidden', innerWidth <= 760);
const initialMode = new URLSearchParams(location.search).get('mode');
let zoomPercent = 100;
const versionScreens = new Map();

function applyZoom() {
  ['frame-a','frame-b'].forEach(id => { $(id).style.zoom = String(zoomPercent / 100); });
  $('zoom-level').textContent = zoomPercent + '%';
  $('zoom-out').disabled = zoomPercent <= 50;
  $('zoom-in').disabled = zoomPercent >= 200;
}
$('zoom-out').onclick = () => { zoomPercent = Math.max(50, zoomPercent - 10); applyZoom(); };
$('zoom-in').onclick = () => { zoomPercent = Math.min(200, zoomPercent + 10); applyZoom(); };
$('zoom-reset').onclick = () => { zoomPercent = 100; applyZoom(); };
applyZoom();

function applyRail(hidden) {
  if (hidden && $('rail').contains(document.activeElement)) $('rail-toggle').focus({preventScroll:true});
  $('rail').hidden = hidden;
  $('app').classList.toggle('rail-hidden', hidden);
  $('rail-toggle').setAttribute('aria-expanded', String(!hidden));
  $('rail-toggle').title = t(hidden ? 'showDecks' : 'hideDecks');
  $('rail-toggle').setAttribute('aria-label', $('rail-toggle').title);
}
let railHidden = readHiddenPreference('mg.rail.hidden', innerWidth <= 760);
applyRail(railHidden);
function setRailHidden(hidden) {
  railHidden = hidden; applyRail(hidden);
  saveHiddenPreference('mg.rail.hidden', hidden);
}
$('rail-toggle').onclick = () => setRailHidden(!railHidden);
$('rail-close').onclick = () => setRailHidden(true);
function applyHistory() {
  if (!historyVisible && $('version-section').contains(document.activeElement)) $('history-toggle').focus({preventScroll:true});
  $('version-section').hidden = !historyVisible;
  $('history-toggle').setAttribute('aria-pressed', String(historyVisible));
  $('history-toggle').setAttribute('aria-expanded', String(historyVisible));
  $('history-toggle').classList.toggle('primary', historyVisible);
  $('history-toggle').title = t(historyVisible ? 'hideHistory' : 'showHistory');
  $('history-toggle').setAttribute('aria-label', $('history-toggle').title);
}
function setHistoryVisible(visible) {
  historyVisible = visible; applyHistory();
  saveHiddenPreference('mg.history.hidden', !visible);
}
$('history-toggle').onclick = () => setHistoryVisible(!historyVisible);
$('history-close').onclick = () => setHistoryVisible(false);
applyHistory();
// Fill the browser tab without entering the browser/OS fullscreen mode.
let previewMaximized = false;
const previewBackground = ['sidebar','lib-bar','bar','rail','version-section'];
const previewBackgroundInert = new Map();
function setPreviewMaximized(maximized) {
  if (previewMaximized === maximized) return;
  previewMaximized = maximized;
  for (const id of previewBackground) {
    const element = $(id);
    if (maximized) {
      previewBackgroundInert.set(id,element.inert);
      element.inert = true;
    } else {
      element.inert = previewBackgroundInert.get(id) || false;
    }
  }
  if (!maximized) previewBackgroundInert.clear();
  $('stage').classList.toggle('browser-maximized',maximized);
  const button = $('fullscreen');
  button.innerHTML = icon(maximized ? 'restore' : 'expand');
  button.setAttribute('aria-pressed',String(maximized));
  const label = t(maximized ? 'restorePreview' : 'maximizePreview');
  button.setAttribute('aria-label',label);
  button.title = label;
}
$('fullscreen').onclick = () => setPreviewMaximized(!previewMaximized);
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || !previewMaximized || activeDialog || openMenus.length) return;
  event.preventDefault();
  setPreviewMaximized(false);
});

function visibleRailDecks() {
  const byId = new Map(decks.map(deck => [deck.id,deck]));
  return (railScopeIds || []).map(id => byId.get(id)).filter(Boolean);
}
function renderDecks() {
  const list = $('deck-list'); list.innerHTML = '';
  const visible = visibleRailDecks();
  if (!visible.length) { list.innerHTML = '<div class="empty">' + esc(t(decksLoadFailed ? 'libraryUnavailable' : 'noDecks')) + '</div>'; return; }
  visible.forEach(deck => {
    const item = document.createElement('div'); item.className = 'deck-item' + (deck.id === currentId ? ' active' : ''); item.dataset.deckId = deck.id;
    item.tabIndex = 0; item.setAttribute('role','button'); item.setAttribute('aria-current', deck.id === currentId ? 'true' : 'false');
    item.innerHTML = (deck.thumb_url ? '<img class="thumb" src="' + esc(versionedThumbUrl(deck)) + '" alt="" loading="lazy">' : '<span class="card">' + icon('screen') + '</span>') + '<span class="name">' + esc(deck.name) + '</span>';
    item.onclick = () => { location.hash = deck.id; };
    item.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); item.click(); } };
    list.append(item);
    if (deck.id === currentId && screens.length > 1) {
      const tree = document.createElement('div'); tree.className = 'screen-tree';
      screens.forEach(screen => {
        const row = document.createElement('button'); row.className = 'screen-item' + (screen.key === currentScreenKey ? ' active' : ''); row.dataset.screenKey = screen.key;
        row.setAttribute('aria-pressed', String(screen.key === currentScreenKey)); row.innerHTML = icon('screen') + '<span>' + esc(screen.tag) + '</span>';
        row.onclick = () => selectScreen(screen.key); tree.append(row);
      });
      list.append(tree);
    }
  });
}
function activeVersionNo() { return versionData?.active_version ?? null; }
function selectedVersionNo() { return mode === 'compare' ? versionB : viewingVersion; }
function readyVersions() { return versionData?.items.filter(item => item.status === 'ready') || []; }
function updateViewerActions() {
  $('download-version').disabled = !readyVersions().some(item => item.version === selectedVersionNo());
  const deck = decks.find(item => item.id === currentId);
  $('active-tag').textContent = deck?.active_version ? t('activeVersion',{version:deck.active_version}) : '';
  $('active-tag').hidden = !deck?.active_version;
  $('screen-label').textContent = screens.find(item => item.key === currentScreenKey)?.tag || '';
}
function renderVersions(errorMessage) {
  const list = $('version-list');
  $('version-heading').textContent = t('history');
  $('history-count').textContent = versionData ? t('count',{count:versionData.items.length}) : '';
  updateViewerActions();
  if (errorMessage || !versionData || !versionData.items.length) {
    list.innerHTML = '<div class="empty">' + esc(errorMessage || t(versionData ? 'noVersions' : 'loading')) + '</div>'; return;
  }
  list.innerHTML = '';
  [...versionData.items].reverse().forEach(item => {
    const selectable = item.status === 'ready' && mode === 'single';
    const viewing = selectable && item.version === viewingVersion;
    const card = document.createElement('div'); card.className = 'version-card' + (selectable ? ' selectable' : '') + (viewing ? ' viewing' : ''); card.dataset.version = String(item.version);
    card.innerHTML = '<div class="version-line"><span class="version-number">v' + esc(item.label) + '</span>'
      + (item.is_active ? '<span class="badge active">' + esc(t('active')) + '</span>' : '')
      + (item.version === versionData.latest_version ? '<span class="badge latest">' + esc(t('latest')) + '</span>' : '')
      + '</div><div class="version-meta">' + esc(formatTimestamp(item.created_at))
      + (item.status !== 'ready' ? ' · <span class="status-' + esc(item.status) + '">' + esc(t('status' + item.status[0].toUpperCase() + item.status.slice(1))) + '</span>' : '') + '</div>'
      + (item.note ? '<div class="version-note">' + esc(item.note) + '</div>' : '');
    if (selectable) {
      card.tabIndex = 0; card.setAttribute('role','button'); card.setAttribute('aria-pressed',String(viewing));
      card.onclick = () => viewVersion(item.version);
      card.onkeydown = event => { if (event.target === card && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); viewVersion(item.version); } };
    }
    const actions = document.createElement('div'); actions.className = 'version-actions';
    const note = document.createElement('button'); note.className = 'edit-note'; note.dataset.action = 'edit-note'; note.textContent = t(item.note ? 'editNote' : 'addNote');
    note.onclick = event => { event.stopPropagation(); editVersionNote(currentId, item); }; actions.append(note);
    if (item.status === 'ready' && scope === 'deck' && !item.is_active) {
      const activate = document.createElement('button'); activate.className = 'set-active-btn'; activate.textContent = t('setActive');
      activate.onclick = event => { event.stopPropagation(); setActiveVersion(currentId,item.version); }; actions.append(activate);
    }
    card.append(actions); list.append(card);
  });
}
function screenPath() { return screens.find(screen => screen.key === currentScreenKey)?.viewer_path || ''; }
function versionUrl(version, path = screenPath()) { return '/v/' + encodeURIComponent(currentId) + '/v' + encodeURIComponent(version) + '/' + path; }
function singleFrameUrl(version) { return version != null ? versionUrl(version) : (decks.find(deck => deck.id === currentId)?.viewer_url || '') + screenPath(); }
function updateViewingTag() {
  const item = versionData?.items.find(entry => entry.version === viewingVersion);
  $('viewing-tag').hidden = mode !== 'single' || viewingVersion == null || viewingVersion === activeVersionNo();
  $('viewing-tag').textContent = t('viewingVersion',{version:item?.label || viewingVersion || ''});
}
async function screensForVersion(id, version) {
  const key = id + ':' + version;
  if (!versionScreens.has(key)) versionScreens.set(key, apiRequest('/api/v1/decks/' + encodeURIComponent(id) + '/screens?version=' + encodeURIComponent(version)).then(data => data.items || []).catch(error => { versionScreens.delete(key); throw error; }));
  return versionScreens.get(key);
}
async function setFrame(letter, version, generation) {
  const id = currentId, key = currentScreenKey;
  const frame = $('frame-' + letter), message = $('message-' + letter);
  if (version == null) { frame.removeAttribute('src'); return; }
  try {
    const items = await screensForVersion(id,version);
    if (generation !== frameGeneration || id !== currentId || key !== currentScreenKey) return;
    const screen = items.find(item => item.key === key) || (!key ? items[0] : null);
    message.hidden = Boolean(screen); message.textContent = t('missingScreen');
    if (!screen) { frame.removeAttribute('src'); return; }
    const url = '/v/' + encodeURIComponent(id) + '/v' + encodeURIComponent(version) + '/' + screen.viewer_path;
    if (frame.getAttribute('src') !== url) frame.src = url;
    const entry = versionData?.items.find(item => item.version === version);
    $('label-' + letter).textContent = (mode === 'compare' ? letter.toUpperCase() + ' · ' : '') + (screen.tag || '') + ' · v' + (entry?.label || version);
  } catch (_) {
    if (generation !== frameGeneration || id !== currentId) return;
    frame.removeAttribute('src'); message.hidden = false; message.textContent = t('deckUnavailable');
  }
}
function updateFrames() {
  const generation = ++frameGeneration;
  void setFrame('a',mode === 'compare' ? versionA : viewingVersion,generation);
  if (mode === 'compare') void setFrame('b',versionB,generation);
  else $('frame-b').removeAttribute('src');
  updateViewerActions();
}
function viewVersion(version) {
  if (!readyVersions().some(item => item.version === version)) return;
  mode = 'single'; updateModeButtons(); viewingVersion = version;
  updateFrames(); renderVersions(); updateViewingTag(); renderVersionControls();
}
function setCompareSources() { updateFrames(); }
function renderVersionControls() {
  const options = [...readyVersions()].reverse().map(item => '<option value="' + item.version + '">v' + esc(item.label) + (item.is_active ? ' · ' + esc(t('active')) : '') + '</option>').join('');
  $('version-a').innerHTML = options; $('version-b').innerHTML = options;
  if (versionA != null) $('version-a').value = String(versionA);
  if (versionB != null) $('version-b').value = String(versionB);
  $('compare-controls').hidden = mode !== 'compare' || !readyVersions().length;
}
function updateModeButtons() {
  document.querySelectorAll('#mode-switch button').forEach(button => { button.setAttribute('aria-pressed', String(button.dataset.mode === mode)); });
  $('viewer').classList.toggle('compare',mode === 'compare');
}
function setMode(nextMode) {
  if (mode === nextMode) return;
  mode = nextMode; updateModeButtons();
  if (mode === 'single') viewingVersion = activeVersionNo() || readyVersions().at(-1)?.version || null;
  updateFrames(); renderVersions(); updateViewingTag(); renderVersionControls();
}
async function fetchVersionData(id, nextScope = scope, key = currentScreenKey) {
  const data = await apiRequest('/api/v1/decks/' + encodeURIComponent(id) + (nextScope === 'screen' && key ? '/screens/' + encodeURIComponent(key) : '') + '/versions');
  if (nextScope === 'screen' && key) {
    const items = (data.items || []).map(item => ({version:item.deck_version, label:item.version, status:'ready', is_active:item.is_active, created_at:item.created_at, note:item.note || ''}));
    return {items, active_version:items.find(item => item.is_active)?.version || null, latest_version:items.at(-1)?.version || null};
  }
  return {...data, items:(data.items || []).map(item => ({...item,label:item.version}))};
}
async function loadVersionData({preserveSelection = false} = {}) {
  const id = currentId, key = currentScreenKey, nextScope = scope, generation = ++versionLoadGeneration;
  const previous = {viewingVersion,versionA,versionB};
  versionData = null; ++frameGeneration; renderVersions();
  ['a','b'].forEach(letter => { $('frame-' + letter).removeAttribute('src'); $('message-' + letter).hidden = false; $('message-' + letter).textContent = t('loading'); });
  try {
    const data = await fetchVersionData(id,nextScope,key);
    if (id !== currentId || key !== currentScreenKey || scope !== nextScope || generation !== versionLoadGeneration) return;
    versionData = data;
    const ready = readyVersions();
    const activeIndex = Math.max(0,ready.findIndex(item => item.is_active));
    const exists = version => ready.some(item => item.version === version);
    viewingVersion = preserveSelection && exists(previous.viewingVersion) ? previous.viewingVersion : data.active_version || ready.at(-1)?.version || null;
    versionB = preserveSelection && exists(previous.versionB) ? previous.versionB : ready[activeIndex]?.version || null;
    versionA = preserveSelection && exists(previous.versionA) ? previous.versionA : ready[Math.max(0,activeIndex - 1)]?.version || null;
    renderVersions(); renderVersionControls(); updateViewingTag(); updateFrames();
  } catch (error) {
    if (id === currentId && generation === versionLoadGeneration) {
      renderVersions(t('historyUnavailable'));
      ['a','b'].forEach(letter => { $('message-' + letter).textContent = t('historyUnavailable'); });
    }
  }
}
function setScope(nextScope) {
  if (scope === nextScope) return;
  scope = nextScope;
  document.querySelectorAll('#scope-switch button').forEach(button => { button.setAttribute('aria-pressed',String(button.dataset.scope === scope)); });
  void loadVersionData();
}
function selectScreen(key) {
  if (key === currentScreenKey) return;
  currentScreenKey = key; renderDecks(); updateViewerActions();
  if (scope === 'screen') void loadVersionData(); else updateFrames();
}
async function selectDeck(id, {preferredScreen = null, preferredScope = 'deck'} = {}) {
  const deck = decks.find(item => item.id === id);
  if (!deck) return;
  const generation = ++viewerLoadGeneration;
  ++versionLoadGeneration; ++frameGeneration;
  currentId = id; screens = []; currentScreenKey = null; scope = preferredScope; versionData = null; mode = 'single';
  versionA = null; versionB = null; viewingVersion = null;
  document.querySelector('#bar .title').textContent = deck.name; $('current-crumb').textContent = deck.name; $('current-crumb').title = deck.name;
  $('viewer-group').textContent = groupPath(deck.group_id) || t('ungrouped'); $('viewer-group').title = $('viewer-group').textContent;
  $('frame-a').removeAttribute('src'); $('frame-b').removeAttribute('src'); $('message-a').hidden = false; $('message-a').textContent = t('loading');
  $('scope-switch').hidden = true; updateModeButtons(); renderVersionControls(); renderDecks(); renderVersions(); updateViewingTag();
  try {
    const data = await apiRequest('/api/v1/decks/' + encodeURIComponent(id) + '/screens');
    if (generation !== viewerLoadGeneration || id !== currentId) return;
    screens = data.items || [];
    if (data.version) versionScreens.set(id + ':' + data.version,Promise.resolve(screens));
    currentScreenKey = screens.some(screen => screen.key === preferredScreen) ? preferredScreen : screens[0]?.key || null;
    $('scope-switch').hidden = screens.length <= 1;
    if (screens.length <= 1) scope = 'deck';
  } catch (_) {
    if (generation !== viewerLoadGeneration || id !== currentId) return;
    currentScreenKey = null;
  }
  document.querySelectorAll('#scope-switch button').forEach(button => { button.setAttribute('aria-pressed',String(button.dataset.scope === scope)); });
  renderDecks(); await loadVersionData();
  if (generation !== viewerLoadGeneration || id !== currentId) return;
  if (initialMode === 'compare') setMode('compare');
}
async function setActiveVersion(id, version) {
  const generation = viewerLoadGeneration;
  try {
    const updated = await apiRequest('/api/v1/decks/' + encodeURIComponent(id) + '/versions/' + version + '/activate',{method:'POST'});
    const index = decks.findIndex(deck => deck.id === id); if (index !== -1) decks[index] = {...decks[index],...normalizeDeck(updated)};
    if (generation !== viewerLoadGeneration || currentId !== id) return;
    versionScreens.delete(id + ':' + version);
    await selectDeck(id,{preferredScreen:currentScreenKey});
    announceLibrary(t('activeChanged'));
  } catch (error) { if (currentId === id) await loadVersionData(); await showFailureAlert(error); }
}
async function editVersionNote(id, item) {
  const pending = openGenericDialog({title:t('versionNote') + ' · v' + item.label, confirmText:t('save'), bodyHtml:'<label class="dialog-field">' + esc(t('versionNote')) + '<textarea id="dialog-note" data-dialog-field="note" maxlength="4000" placeholder="' + esc(t('notePlaceholder')) + '">' + esc(item.note || '') + '</textarea></label>'});
  const result = await pending;
  if (result.action !== 'confirm') return;
  try {
    const updated = await apiRequest('/api/v1/decks/' + encodeURIComponent(id) + '/versions/' + item.version,jsonRequest('PATCH',{note:result.values.note}));
    if (currentId === id && versionData) { const current = versionData.items.find(version => version.version === item.version); if (current) current.note = updated.note; renderVersions(); }
    announceLibrary(t('noteSaved'));
  } catch (error) { await showFailureAlert(error); }
}
async function refreshViewerAfterUpload(id) {
  const preferredScreen = currentScreenKey, preferredScope = scope;
  const response = await apiRequest('/api/v1/decks');
  if (currentId !== id) return;
  decks = (response.items || []).map(normalizeDeck); decksLoaded = true;
  await selectDeck(id,{preferredScreen,preferredScope});
}
function downloadSelectedVersion() {
  const version = selectedVersionNo();
  if (!currentId || !readyVersions().some(item => item.version === version)) return;
  const link = document.createElement('a'); link.href = '/api/v1/decks/' + encodeURIComponent(currentId) + '/download?version=' + version; link.download = ''; document.body.append(link); link.click(); link.remove();
}
document.querySelectorAll('#mode-switch button').forEach(button => { button.onclick = () => setMode(button.dataset.mode); });
document.querySelectorAll('#scope-switch button').forEach(button => { button.onclick = () => setScope(button.dataset.scope); });
$('version-a').onchange = event => { versionA = Number(event.target.value); updateFrames(); };
$('version-b').onchange = event => { versionB = Number(event.target.value); updateFrames(); };
$('swap').onclick = () => { [versionA,versionB] = [versionB,versionA]; renderVersionControls(); updateFrames(); };
$('copy').onclick = () => copyText(location.origin + '/v/' + encodeURIComponent(currentId) + '/' + screenPath());
$('download-version').onclick = downloadSelectedVersion;
$('viewer-more').onclick = () => openMenu($('viewer-more'),[
  {label:t('uploadVersion'),action:'upload-version',disabled:uploadInFlight,onClick:uploadNewVersion},
  {label:t('download'),action:'download',disabled:!readyVersions().length,onClick:downloadSelectedVersion},
  {label:t('openNewTab'),action:'open',onClick:() => window.open(singleFrameUrl(selectedVersionNo()),'_blank','noopener')}
]);
$('to-library').onclick = () => { location.hash = ''; };
