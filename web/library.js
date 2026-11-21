'use strict';

const PAGE_SIZE = 24;
const FAVORITE_MIGRATION_KEY = 'mg.library.favorite-migration.v1';
let libraryData = null;
let libraryLoadGeneration = 0;
let organizationMoveInFlight = false;
let dragState = null;
let suppressDeckClickUntil = 0;
let migrationInFlight = null;
const pendingFavorites = new Set();
const libraryState = {nav:'all',sort:'updated',layout:'grid',visibleCount:PAGE_SIZE,selectMode:false,selected:new Set(),lastSelected:null};
let libZoomPercent = 100;

function applyLibZoom() {
  $('lib-scroll').style.zoom = String(libZoomPercent / 100);
  $('lib-zoom-level').textContent = libZoomPercent + '%';
  $('lib-zoom-out').disabled = libZoomPercent <= 50;
  $('lib-zoom-in').disabled = libZoomPercent >= 200;
}
$('lib-zoom-out').onclick = () => { libZoomPercent = Math.max(50,libZoomPercent - 10); applyLibZoom(); };
$('lib-zoom-in').onclick = () => { libZoomPercent = Math.min(200,libZoomPercent + 10); applyLibZoom(); };
$('lib-zoom-reset').onclick = () => { libZoomPercent = 100; applyLibZoom(); };
applyLibZoom();

function allLibraryDecks(data = libraryData) {
  return data ? [...data.groups.flatMap(group => group.decks),...data.ungrouped.decks] : [];
}
function findLibraryDeck(id) { return allLibraryDecks().find(deck => deck.id === id) || null; }
function libraryDecksFor(groupId, data = libraryData) {
  return !data ? [] : groupId === null ? data.ungrouped.decks : data.groups.find(group => group.id === groupId)?.decks || [];
}
function groupName(groupId) { return groupId === null ? t('ungrouped') : libraryData?.groups.find(group => group.id === groupId)?.name || ''; }
function dialogGroupValue(groupId) { return groupId === null ? 'ungrouped' : 'group:' + groupId; }
function parseDialogGroupValue(value) { return value === 'ungrouped' ? null : value.slice(6); }
function currentGroupId() { return libraryState.nav === 'ungrouped' ? null : libraryState.nav.startsWith('group:') ? libraryState.nav.slice(6) : undefined; }

async function migrateLegacyFavorites(data) {
  try { if (localStorage.getItem(FAVORITE_MIGRATION_KEY) === 'true') return data; } catch (_) { return data; }
  const legacyIds = new Set(readJsonList('mg.library.pinned'));
  const ids = allLibraryDecks(data).filter(deck => legacyIds.has(deck.id) && !deck.favorite).map(deck => deck.id);
  let updated = data;
  try {
    for (let offset = 0; offset < ids.length; offset += 1000) updated = await apiRequest('/api/v1/decks/favorites',jsonRequest('PUT',{deck_ids:ids.slice(offset,offset + 1000),favorite:true}));
    localStorage.setItem(FAVORITE_MIGRATION_KEY,'true');
    return updated;
  } catch (_) { announceLibrary(t('favoritesMigrationFailed')); return data; }
}
async function loadLibrary() {
  const generation = ++libraryLoadGeneration;
  decksLoaded = false;
  $('library-refresh').disabled = true;
  try {
    let data = await apiRequest('/api/v1/library');
    if (generation !== libraryLoadGeneration) return false;
    if (migrationInFlight) {
      await migrationInFlight;
      data = await apiRequest('/api/v1/library');
    } else {
      migrationInFlight = migrateLegacyFavorites(data).finally(() => { migrationInFlight = null; });
      data = await migrationInFlight;
    }
    if (generation !== libraryLoadGeneration) return false;
    libraryData = data;
    const known = new Set(allLibraryDecks().map(deck => deck.id));
    libraryState.selected = new Set([...libraryState.selected].filter(id => known.has(id)));
    if (libraryState.nav.startsWith('group:') && !data.groups.some(group => libraryState.nav === 'group:' + group.id)) libraryState.nav = 'ungrouped';
    renderLibrary(); return true;
  } catch (_) {
    if (generation !== libraryLoadGeneration) return false;
    $('lib-groups').innerHTML = '<div class="lib-empty">' + esc(t('libraryUnavailable')) + '<br><button class="button" id="retry-library">' + esc(t('retry')) + '</button></div>';
    $('library-pager').hidden = true;
    $('retry-library').onclick = loadLibrary;
    return false;
  } finally { if (generation === libraryLoadGeneration) $('library-refresh').disabled = false; }
}
function filteredDecks() {
  const query = $('lib-search').value.trim().toLocaleLowerCase();
  let items = allLibraryDecks();
  if (query) items = items.filter(deck => [deck.name,groupName(deck.group_id),deck.id].some(value => String(value).toLocaleLowerCase().includes(query)));
  else if (libraryState.nav === 'pinned') items = items.filter(deck => deck.favorite);
  else if (libraryState.nav === 'recent') items = items.filter(deck => deck.last_viewed_at);
  else if (currentGroupId() !== undefined) items = libraryDecksFor(currentGroupId());
  items = [...items];
  const byName = (left,right) => left.name.localeCompare(right.name,'ko',{numeric:true}) || left.id.localeCompare(right.id);
  if (!query && libraryState.nav === 'recent') items.sort((left,right) => String(right.last_viewed_at).localeCompare(String(left.last_viewed_at)) || byName(left,right));
  else if (libraryState.sort === 'name') items.sort(byName);
  else if (libraryState.sort === 'versions') items.sort((left,right) => right.total_versions - left.total_versions || byName(left,right));
  else if (libraryState.sort === 'updated') items.sort((left,right) => String(right.latest_version_at || right.updated_at || right.created_at).localeCompare(String(left.latest_version_at || left.updated_at || left.created_at)) || byName(left,right));
  return items;
}
function groupColor(id) { let hash = 0; for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0; return ['#7c9dff','#dfad78','#5f9dcb','#b49aee','#7bc2b3','#df8d9b'][hash % 6]; }
function updateProjectNav() {
  if (!libraryData) return;
  $('nav-total').textContent = allLibraryDecks().length;
  $('nav-favorite-count').textContent = allLibraryDecks().filter(deck => deck.favorite).length || '';
  $('nav-ungrouped-count').textContent = libraryData.ungrouped.decks.length;
  const nav = $('project-nav'); nav.innerHTML = '';
  libraryData.groups.forEach(group => {
    const row = document.createElement('div'); row.className = 'project-item'; row.dataset.groupId = group.id;
    const button = document.createElement('button'); button.className = 'side-link'; button.dataset.nav = 'group:' + group.id; button.draggable = true;
    button.innerHTML = '<span class="project-dot" style="background:' + groupColor(group.id) + '"></span><span class="project-name">' + esc(group.name) + '</span><span class="count">' + group.decks.length + '</span>';
    button.title = group.name; button.onclick = () => setNav('group:' + group.id);
    button.ondragstart = event => startLibraryDrag(event,{kind:'group',id:group.id},row); button.ondragend = finishLibraryDrag;
    const menu = document.createElement('button'); menu.className = 'lib-group-kebab'; menu.setAttribute('aria-label',t('groupActions')); menu.setAttribute('aria-haspopup','menu'); menu.textContent = '⋮';
    menu.onclick = () => openMenu(menu,[{label:t('rename'),action:'rename',onClick:() => renameGroup(group)},{label:t('reorder'),action:'reorder',onClick:() => chooseGroupReorder(group)},{label:t('delete'),action:'delete',destructive:true,onClick:() => deleteGroup(group)}]);
    row.append(button,menu);
    row.ondragover = event => {
      if (!dragState) return;
      event.preventDefault(); event.dataTransfer.dropEffect = 'move';
      showDropHighlight(row,dragState.kind === 'group' ? (event.clientY < row.getBoundingClientRect().top + row.offsetHeight / 2 ? 'drop-before' : 'drop-after') : 'deck-drop-target');
      autoScrollProjects(event);
    };
    row.ondrop = event => {
      if (dragState?.kind === 'group') dropGroup(event,group.id,event.clientY < row.getBoundingClientRect().top + row.offsetHeight / 2 ? 'before' : 'after');
      else dropDecks(event,group.id,null,'end');
    };
    nav.append(row);
  });
  document.querySelectorAll('.side-link[data-nav]').forEach(button => {
    const active = button.dataset.nav === libraryState.nav;
    button.classList.toggle('active',active); if (active) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
  });
}
function groupOptions() {
  return (libraryData?.groups || []).map(group => '<option value="group:' + esc(group.id) + '">' + esc(group.name) + '</option>').join('') + '<option value="ungrouped">' + esc(t('ungrouped')) + '</option>';
}
function renderSelection() {
  const count = libraryState.selected.size, visible = libraryState.selectMode || count > 0;
  document.body.classList.toggle('selection-mode',visible);
  $('selection-toggle').setAttribute('aria-pressed',String(libraryState.selectMode));
  $('selection-toggle').classList.toggle('primary',libraryState.selectMode);
  $('batch-bar').classList.toggle('has-selection',count > 0);
  $('select-all').hidden = !visible; $('selection-count').hidden = !count; $('selection-clear').hidden = !visible;
  $('selection-count').textContent = t('selected',{count}); $('move-target').hidden = !count; $('bulk-move').hidden = !count;
  $('select-all').disabled = organizationMoveInFlight || !filteredDecks().length;
  $('bulk-move').disabled = organizationMoveInFlight || !count;
  const target = $('move-target').value; $('move-target').innerHTML = groupOptions();
  if (Array.from($('move-target').options).some(option => option.value === target)) $('move-target').value = target;
  $('move-target').disabled = organizationMoveInFlight;
  document.querySelectorAll('.lib-card[data-deck-id]').forEach(card => {
    const selected = libraryState.selected.has(card.dataset.deckId); card.classList.toggle('selected',selected);
    const button = card.querySelector('.deck-select'); button.setAttribute('aria-checked',String(selected)); button.textContent = selected ? '✓' : '';
  });
}
function renderLibrary() {
  if (!libraryData) return;
  updateProjectNav();
  const query = $('lib-search').value.trim(), items = filteredDecks();
  const name = query ? t('searchResults') : currentGroupId() !== undefined ? groupName(currentGroupId()) : t({all:'libraryTitle',recent:'recent',pinned:'favorites'}[libraryState.nav] || 'libraryTitle');
  $('library-title').textContent = name; $('library-count').textContent = t('count',{count:items.length});
  $('library-desc').textContent = query ? t('searchDescription') : currentGroupId() !== undefined ? t('groupDescription') : t({all:'libraryDescription',recent:'recentDescription',pinned:'favoritesDescription'}[libraryState.nav] || 'libraryDescription');
  if (!location.hash) $('current-crumb').textContent = query ? t('searchResults') : currentGroupId() !== undefined ? groupName(currentGroupId()) : t({all:'all',recent:'recent',pinned:'favorites'}[libraryState.nav] || 'all');
  $('toolbar-label').textContent = t('shown',{shown:Math.min(libraryState.visibleCount,items.length),total:items.length});
  $('lib-sort').value = libraryState.sort; $('lib-sort').disabled = libraryState.nav === 'recent' && !query;
  $('layout-grid').setAttribute('aria-pressed',String(libraryState.layout === 'grid')); $('layout-list').setAttribute('aria-pressed',String(libraryState.layout === 'list'));
  const container = $('lib-groups'); container.className = libraryState.layout === 'grid' ? 'deck-grid' : 'deck-list'; container.innerHTML = '';
  items.slice(0,libraryState.visibleCount).forEach(deck => container.append(buildDeckCard(deck)));
  if (!items.length) {
    const key = query ? 'noMatches' : libraryState.nav === 'pinned' ? 'noFavorites' : libraryState.nav === 'recent' ? 'noRecent' : libraryState.nav === 'ungrouped' ? 'emptyUngrouped' : currentGroupId() !== undefined ? 'emptyGroup' : 'noDecks';
    container.innerHTML = '<div class="lib-empty">' + esc(t(key)) + '</div>';
  }
  $('library-pager').hidden = items.length <= libraryState.visibleCount;
  $('pager-message').textContent = t('shown',{shown:Math.min(libraryState.visibleCount,items.length),total:items.length});
  renderSelection();
}
function selectDeckItem(id, event = {}) {
  if (event.shiftKey && libraryState.lastSelected) {
    const ids = filteredDecks().map(deck => deck.id), first = ids.indexOf(libraryState.lastSelected), last = ids.indexOf(id);
    if (first >= 0 && last >= 0) ids.slice(Math.min(first,last),Math.max(first,last) + 1).forEach(item => libraryState.selected.add(item));
    else libraryState.selected.add(id);
  } else {
    if (libraryState.selected.has(id)) libraryState.selected.delete(id); else libraryState.selected.add(id);
    libraryState.lastSelected = id;
  }
  renderSelection();
}
function orderedSelection() {
  const ids = [...filteredDecks(),...allLibraryDecks()].map(deck => deck.id);
  return [...new Set(ids)].filter(id => libraryState.selected.has(id));
}
function buildDeckCard(deck) {
  const card = document.createElement('article'); card.className = 'lib-card'; card.dataset.deckId = deck.id; card.dataset.groupId = deck.group_id || ''; card.dataset.organizable = 'true'; card.draggable = true; card.tabIndex = 0; card.setAttribute('aria-label',deck.name);
  const thumb = document.createElement('div'); thumb.className = 'lib-card-thumb';
  if (deck.thumb_url) {
    const image = document.createElement('img'); image.src = versionedThumbUrl(deck); image.alt = ''; image.loading = 'lazy'; image.draggable = false;
    image.onerror = () => { image.remove(); thumb.insertAdjacentHTML('afterbegin',icon('screen')); }; thumb.append(image);
  } else thumb.innerHTML = icon('screen');
  const badge = document.createElement('span'); badge.className = 'thumb-overlay'; badge.textContent = groupName(deck.group_id); thumb.append(badge);
  const selection = document.createElement('button'); selection.className = 'deck-select'; selection.setAttribute('role','checkbox'); selection.setAttribute('aria-label',t('selectDeck',{name:deck.name})); selection.setAttribute('aria-checked',String(libraryState.selected.has(deck.id))); selection.draggable = false;
  selection.onclick = event => { event.stopPropagation(); selectDeckItem(deck.id,event); };
  const body = document.createElement('div'); body.className = 'card-body';
  body.innerHTML = '<div class="card-top"><div class="lib-card-name">' + esc(deck.name) + '</div></div><div class="lib-card-meta"><span class="version-badge">v' + esc(deck.active_version) + ' / ' + esc(deck.total_versions || deck.latest_version) + '</span><span class="screen-count">' + esc(t('screenCount',{count:deck.screen_count || 1})) + '</span></div><div class="card-date">' + esc(formatTimestamp(deck.latest_version_at || deck.updated_at || deck.created_at)) + '</div>';
  const pin = document.createElement('button'); pin.className = 'lib-pin' + (deck.favorite ? ' pinned' : ''); pin.draggable = false; pin.innerHTML = icon('star');
  pin.disabled = pendingFavorites.has(deck.id); pin.setAttribute('aria-pressed',String(Boolean(deck.favorite))); pin.setAttribute('aria-label',t(deck.favorite ? 'removeFavorite' : 'addFavorite')); pin.title = pin.getAttribute('aria-label');
  pin.onclick = event => { event.stopPropagation(); void togglePin(deck.id); };
  const menu = document.createElement('button'); menu.className = 'lib-kebab'; menu.draggable = false; menu.textContent = '⋮'; menu.setAttribute('aria-label',t('deckActions')); menu.setAttribute('aria-haspopup','menu');
  menu.onclick = event => { event.stopPropagation(); openDeckMenu(menu,deck); };
  body.querySelector('.card-top').append(pin,menu); card.append(thumb,selection,body);
  card.onclick = event => {
    if (Date.now() < suppressDeckClickUntil || event.target.closest('button')) return;
    if (libraryState.selectMode || event.ctrlKey || event.metaKey || event.shiftKey) selectDeckItem(deck.id,event);
    else location.hash = deck.id;
  };
  card.onkeydown = event => {
    if (event.target !== card) return;
    if (event.key === ' ' || (event.key === 'Enter' && (libraryState.selectMode || event.ctrlKey || event.metaKey))) { event.preventDefault(); selectDeckItem(deck.id,event); }
    else if (event.key === 'Enter') { event.preventDefault(); location.hash = deck.id; }
  };
  card.ondragstart = event => {
    if (event.target.closest('button')) { event.preventDefault(); return; }
    startLibraryDrag(event,{kind:'deck',id:deck.id,ids:libraryState.selected.has(deck.id) ? orderedSelection() : [deck.id]},card);
  };
  card.ondragend = finishLibraryDrag;
  card.ondragover = event => {
    if (dragState?.kind !== 'deck') return;
    event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = 'move';
    if (!dragState.ids.includes(deck.id)) showDropHighlight(card,cardDropPosition(event,card) === 'before' ? 'drop-before' : 'drop-after');
  };
  card.ondrop = event => dropDecks(event,deck.group_id,deck.id,cardDropPosition(event,card));
  return card;
}
function cardDropPosition(event,card) {
  const rect = card.getBoundingClientRect();
  return event.clientY < rect.top + rect.height / 2 ? 'before' : 'after';
}
function openDeckMenu(anchor,deck) {
  openMenu(anchor,[
    {label:t('rename'),action:'rename',onClick:() => renameDeck(deck)},
    {label:t('move'),action:'move',onClick:() => chooseDeckMove(deck)},
    {label:t('copyLink'),action:'copy',onClick:() => copyDeckLink(deck)},
    {label:t(deck.favorite ? 'removeFavorite' : 'addFavorite'),action:'favorite',onClick:() => togglePin(deck.id)},
    {separator:true},{label:t('delete'),action:'delete',destructive:true,onClick:() => deleteDeck(deck)}
  ]);
}
function focusLibraryItem(kind,id,control = '.lib-kebab') {
  const selector = kind === 'group' ? '#project-nav .project-item[data-group-id="' + CSS.escape(id) + '"] .lib-group-kebab'
    : '#lib-groups .lib-card[data-deck-id="' + CSS.escape(id) + '"] ' + control;
  const target = document.querySelector(selector);
  if (target) target.focus({preventScroll:true});
}
function focusScope(groupId) {
  const target = groupId === null ? $('nav-ungrouped') : document.querySelector('#project-nav .project-item[data-group-id="' + CSS.escape(groupId) + '"] .side-link');
  target?.focus({preventScroll:true});
}
async function togglePin(id) {
  const deck = findLibraryDeck(id); if (!deck || pendingFavorites.has(id)) return;
  const favorite = !deck.favorite; pendingFavorites.add(id); renderSelection();
  document.querySelectorAll('.lib-card[data-deck-id="' + CSS.escape(id) + '"] .lib-pin').forEach(button => { button.disabled = true; });
  try {
    await apiRequest('/api/v1/decks/' + encodeURIComponent(id) + '/favorite',jsonRequest('PUT',{favorite}));
    await loadLibrary(); announceLibrary(t(favorite ? 'favoriteSaved' : 'favoriteRemoved'));
  } catch (error) { await loadLibrary(); await showFailureAlert(error); }
  finally {
    pendingFavorites.delete(id);
    if (libraryData) { renderLibrary(); focusLibraryItem('deck',id,'.lib-pin'); }
    if (!document.querySelector('#lib-groups .lib-card[data-deck-id="' + CSS.escape(id) + '"]')) $('nav-pinned').focus({preventScroll:true});
  }
}
function setNav(nav) {
  libraryState.nav = nav; libraryState.visibleCount = PAGE_SIZE; $('lib-search').value = '';
  libraryState.sort = nav.startsWith('group:') || nav === 'ungrouped' ? 'manual' : 'updated';
  closeMobileMenu(); closeMenu();
  if (location.hash) location.hash = ''; else renderLibrary();
  $('lib-scroll').scrollTop = 0;
}

function clearDropHighlights() { document.querySelectorAll('.drop-before,.drop-after,.deck-drop-target').forEach(element => element.classList.remove('drop-before','drop-after','deck-drop-target')); }
function showDropHighlight(element,className) { clearDropHighlights(); element.classList.add(className); }
function autoScrollProjects(event) {
  const nav = $('project-nav'), rect = nav.getBoundingClientRect();
  if (event.clientY < rect.top + 35) nav.scrollTop -= 12;
  else if (event.clientY > rect.bottom - 35) nav.scrollTop += 12;
}
function startLibraryDrag(event,state,element) {
  if (organizationMoveInFlight) { event.preventDefault(); return; }
  closeMenu(); dragState = {...state,sourceElement:element}; element.classList.add('drag-source'); $('shell').classList.add('dragging');
  event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain',state.ids ? state.ids.join(',') : state.id);
}
function finishLibraryDrag() {
  if (dragState?.kind === 'deck') suppressDeckClickUntil = Date.now() + 300;
  dragState?.sourceElement?.classList.remove('drag-source'); dragState = null; clearDropHighlights(); $('shell').classList.remove('dragging');
}
function isBulkMoveNoop(ids,groupId,index) {
  if (!ids.length || ids.some(id => findLibraryDeck(id)?.group_id !== groupId)) return false;
  const before = libraryDecksFor(groupId).map(deck => deck.id), after = before.filter(id => !ids.includes(id)); after.splice(Math.min(index,after.length),0,...ids);
  return JSON.stringify(before) === JSON.stringify(after);
}
function dropDecks(event,groupId,targetId,position) {
  if (dragState?.kind !== 'deck') return;
  event.preventDefault(); event.stopPropagation();
  const ids = [...dragState.ids], self = targetId && ids.includes(targetId);
  const target = libraryDecksFor(groupId).map(deck => deck.id).filter(id => !ids.includes(id));
  const targetIndex = target.indexOf(targetId);
  const index = targetId === null ? (position === 'start' ? 0 : target.length) : targetIndex < 0 ? target.length : targetIndex + (position === 'after' ? 1 : 0);
  finishLibraryDrag();
  if (self || isBulkMoveNoop(ids,groupId,index)) return;
  if (targetId) libraryState.sort = 'manual';
  void dispatchOrganizationMove({kind:'decks',ids,groupId,index});
}
function dropGroup(event,targetId,position) {
  if (dragState?.kind !== 'group') return;
  event.preventDefault(); event.stopPropagation(); const id = dragState.id; finishLibraryDrag();
  if (id === targetId) return;
  const others = libraryData.groups.map(group => group.id).filter(groupId => groupId !== id);
  const index = others.indexOf(targetId) + (position === 'after' ? 1 : 0);
  void dispatchOrganizationMove({kind:'group',id,index});
}
async function dispatchOrganizationMove(command) {
  if (!libraryData || organizationMoveInFlight) return false;
  const ids = command.ids || [command.id];
  if (command.kind === 'group' ? libraryData.groups.findIndex(group => group.id === command.id) === command.index : isBulkMoveNoop(ids,command.groupId,command.index)) return true;
  organizationMoveInFlight = true; renderSelection();
  try {
    if (command.kind === 'group') await apiRequest('/api/v1/groups/' + encodeURIComponent(command.id) + '/move',jsonRequest('POST',{index:command.index}));
    else await apiRequest('/api/v1/decks/move',jsonRequest('POST',{deck_ids:ids,group_id:command.groupId,index:command.index}));
    if (command.kind !== 'group') ids.forEach(id => libraryState.selected.delete(id));
    await loadLibrary();
    announceLibrary(command.kind === 'group' ? t('reordered') : t('moved',{count:ids.length}));
    if (command.kind === 'group') focusLibraryItem('group',command.id);
    else if (ids.length === 1 && document.querySelector('#lib-groups .lib-card[data-deck-id="' + CSS.escape(ids[0]) + '"]')) focusLibraryItem('deck',ids[0]);
    else focusScope(command.groupId);
    return true;
  } catch (error) {
    await loadLibrary(); await showFailureAlert(error,t('moveFailed')); return false;
  } finally { organizationMoveInFlight = false; if (libraryData) renderSelection(); }
}
function fillDeckPositionOptions(select,ids,groupId,originalGroupId) {
  const siblings = libraryDecksFor(groupId).filter(deck => !ids.includes(deck.id));
  select.innerHTML = siblings.map((deck,index) => '<option value="' + index + '">' + (index + 1) + ' · ' + esc(t('beforeItem',{name:deck.name})) + '</option>').join('') + '<option value="' + siblings.length + '">' + (siblings.length + 1) + ' · ' + esc(t('endSection')) + '</option>';
  const original = libraryDecksFor(groupId).findIndex(deck => deck.id === ids[0]);
  select.value = String(groupId === originalGroupId && original >= 0 ? original : siblings.length);
}
async function chooseDeckMove(deck) {
  if (!libraryData || organizationMoveInFlight) return;
  const ids = libraryState.selected.has(deck.id) && libraryState.selected.size > 1 ? orderedSelection() : [deck.id];
  const pending = openGenericDialog({title:t('moveDeck'), confirmText:t('move'), bodyHtml:'<label class="dialog-field">' + esc(t('destination')) + '<select id="dialog-move-group" data-dialog-field="group">' + groupOptions() + '</select></label><label class="dialog-field">' + esc(t('position')) + '<select id="dialog-move-index" data-dialog-field="index"></select></label>'});
  const group = $('dialog-move-group'), index = $('dialog-move-index'); group.value = dialogGroupValue(deck.group_id);
  const update = () => fillDeckPositionOptions(index,ids,parseDialogGroupValue(group.value),deck.group_id);
  group.onchange = update; update();
  const result = await pending;
  if (result.action === 'confirm') {
    const groupId = parseDialogGroupValue(result.values.group);
    if (currentGroupId() === groupId) libraryState.sort = 'manual';
    await dispatchOrganizationMove({kind:'decks',ids,groupId,index:Number(result.values.index)});
  }
}
async function chooseGroupReorder(group) {
  if (organizationMoveInFlight) return;
  const others = libraryData.groups.filter(item => item.id !== group.id);
  const options = others.map((item,index) => '<option value="' + index + '">' + (index + 1) + ' · ' + esc(t('beforeItem',{name:item.name})) + '</option>').join('') + '<option value="' + others.length + '">' + (others.length + 1) + ' · ' + esc(t('endSection')) + '</option>';
  const pending = openGenericDialog({title:t('reorder'),confirmText:t('move'),bodyHtml:'<label class="dialog-field">' + esc(t('position')) + '<select id="dialog-group-index" data-dialog-field="index">' + options + '</select></label>'});
  $('dialog-group-index').value = String(libraryData.groups.findIndex(item => item.id === group.id));
  const result = await pending;
  if (result.action === 'confirm') await dispatchOrganizationMove({kind:'group',id:group.id,index:Number(result.values.index)});
}
async function createGroup() {
  const name = await promptName({title:t('newGroup'),label:t('groupName')}); if (name === null) return;
  try { const group = await apiRequest('/api/v1/groups',jsonRequest('POST',{name})); await loadLibrary(); focusScope(group.id); }
  catch (error) { await loadLibrary(); await showFailureAlert(error); }
}
async function renameGroup(group) {
  const name = await promptName({title:t('renameGroup'),label:t('groupName'),initialValue:group.name}); if (name === null) return;
  try { await apiRequest('/api/v1/groups/' + encodeURIComponent(group.id),jsonRequest('PATCH',{name})); await loadLibrary(); focusLibraryItem('group',group.id); }
  catch (error) { await loadLibrary(); await showFailureAlert(error); }
}
async function deleteGroup(group) {
  if (!await confirmDestructive({title:t('deleteGroup'),bodyHtml:'<p><strong>' + esc(group.name) + '</strong></p><p>' + esc(t('deleteGroupDescription')) + '</p>',confirmText:t('deleteGroup')})) return;
  try { await apiRequest('/api/v1/groups/' + encodeURIComponent(group.id),{method:'DELETE'}); await loadLibrary(); focusScope(null); }
  catch (error) { await loadLibrary(); await showFailureAlert(error); }
}
async function renameDeck(deck) {
  const name = await promptName({title:t('renameDeck'),label:t('deckName'),initialValue:deck.name}); if (name === null) return;
  try { await apiRequest('/api/v1/decks/' + encodeURIComponent(deck.id),jsonRequest('PATCH',{name})); await loadLibrary(); focusLibraryItem('deck',deck.id); }
  catch (error) { await loadLibrary(); await showFailureAlert(error); }
}
async function deleteDeck(deck) {
  if (!await confirmDestructive({title:t('deleteDeck'),bodyHtml:'<p><strong>' + esc(deck.name) + '</strong></p><p>' + esc(t('deleteDeckDescription')) + '</p>',confirmText:t('deleteDeck')})) return;
  try { await apiRequest('/api/v1/decks/' + encodeURIComponent(deck.id),{method:'DELETE'}); await loadLibrary(); (document.querySelector('#lib-groups .lib-card') || $('nav-all')).focus({preventScroll:true}); }
  catch (error) { await loadLibrary(); await showFailureAlert(error); }
}

function closeMobileMenu() { $('shell').classList.remove('menu-open'); $('mobile-shade').hidden = true; $('mobile-menu').setAttribute('aria-expanded','false'); }
$('mobile-menu').onclick = () => { const open = !$('shell').classList.contains('menu-open'); $('shell').classList.toggle('menu-open',open); $('mobile-shade').hidden = !open; $('mobile-menu').setAttribute('aria-expanded',String(open)); };
$('mobile-shade').onclick = closeMobileMenu;
document.querySelectorAll('#sidebar > .side-link,#nav-ungrouped').forEach(button => { button.onclick = () => setNav(button.dataset.nav); });
$('nav-ungrouped').ondragover = event => { if (dragState?.kind === 'deck') { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; showDropHighlight($('nav-ungrouped'),'deck-drop-target'); } };
$('nav-ungrouped').ondrop = event => dropDecks(event,null,null,'end');
$('lib-groups').ondragover = event => {
  if (dragState?.kind !== 'deck' || currentGroupId() === undefined || event.target.closest('.lib-card')) return;
  event.preventDefault(); showDropHighlight($('lib-groups'),'deck-drop-target');
};
$('lib-groups').ondrop = event => { if (!event.target.closest('.lib-card') && currentGroupId() !== undefined) dropDecks(event,currentGroupId(),null,'end'); };
$('lib-scroll').addEventListener('dragover',event => {
  if (dragState?.kind !== 'deck') return;
  const rect = $('lib-scroll').getBoundingClientRect();
  if (event.clientY < rect.top + 45) $('lib-scroll').scrollTop -= 12;
  if (event.clientY > rect.bottom - 45) $('lib-scroll').scrollTop += 12;
});
$('selection-toggle').onclick = () => { libraryState.selectMode = !libraryState.selectMode; if (!libraryState.selectMode) libraryState.selected.clear(); renderSelection(); };
$('select-all').onclick = () => { filteredDecks().forEach(deck => libraryState.selected.add(deck.id)); renderSelection(); };
$('selection-clear').onclick = () => { libraryState.selected.clear(); libraryState.lastSelected = null; libraryState.selectMode = false; renderSelection(); };
$('bulk-move').onclick = () => { const ids = orderedSelection(), groupId = parseDialogGroupValue($('move-target').value); const index = libraryDecksFor(groupId).filter(deck => !ids.includes(deck.id)).length; void dispatchOrganizationMove({kind:'decks',ids,groupId,index}); };
$('lib-new-group').onclick = createGroup;
$('library-refresh').onclick = () => { if (location.hash) void openDeckFromHash(location.hash.slice(1),++navGeneration); else void loadLibrary(); };
$('library-more').onclick = () => openMenu($('library-more'),[{label:t('uploadDeck'),action:'upload',disabled:uploadInFlight,onClick:uploadNewDeck},{label:t('refresh'),action:'refresh',onClick:loadLibrary}]);
$('lib-search').oninput = () => { libraryState.nav = 'all'; libraryState.visibleCount = PAGE_SIZE; if (location.hash) location.hash = ''; else renderLibrary(); };
$('lib-sort').onchange = event => { libraryState.sort = event.target.value; libraryState.visibleCount = PAGE_SIZE; renderLibrary(); };
$('layout-grid').onclick = () => { libraryState.layout = 'grid'; renderLibrary(); };
$('layout-list').onclick = () => { libraryState.layout = 'list'; renderLibrary(); };
$('load-more').onclick = () => { libraryState.visibleCount += PAGE_SIZE; renderLibrary(); };
document.addEventListener('keydown',event => {
  if (activeDialog || event.target.closest('input,textarea,select,[contenteditable="true"]')) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); $('lib-search').focus(); }
  if (!location.hash && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a' && (libraryState.selectMode || libraryState.selected.size)) { event.preventDefault(); $('select-all').click(); }
  if (event.key === 'Escape') { closeMobileMenu(); if (!openMenus.length && !location.hash) $('selection-clear').click(); }
});

let navGeneration = 0;
function showLibraryScreen() {
  $('app').hidden = true; $('library').hidden = false; railScopeIds = null;
  $('lib-zoom-controls').hidden = false;
  ++viewerLoadGeneration; ++versionLoadGeneration; ++frameGeneration;
  currentId = null; $('frame-a').removeAttribute('src'); $('frame-b').removeAttribute('src');
  void loadLibrary();
}
function showViewerScreen() { $('library').hidden = true; $('app').hidden = false; $('lib-zoom-controls').hidden = true; closeMobileMenu(); }
async function ensureDecksLoaded() {
  const data = await apiRequest('/api/v1/decks');
  return (data.items || []).map(normalizeDeck);
}
async function loadRailScope(id) {
  try {
    const data = await apiRequest('/api/v1/library');
    const group = data.groups.find(item => item.decks.some(deck => deck.id === id));
    const ids = group ? group.decks.map(deck => deck.id) : data.ungrouped.decks.some(deck => deck.id === id) ? data.ungrouped.decks.map(deck => deck.id) : [id];
    return {ids,data};
  } catch (_) { return {ids:[id],data:null}; }
}
async function recordRecent(id) {
  try {
    const updated = await apiRequest('/api/v1/decks/' + encodeURIComponent(id) + '/visit',{method:'POST'});
    const deck = findLibraryDeck(id); if (deck) deck.last_viewed_at = updated.last_viewed_at;
  } catch (_) { /* Viewing stays available when management writes are disabled. */ }
}
async function openDeckFromHash(id,generation) {
  let loaded;
  try { loaded = await ensureDecksLoaded(); }
  catch (error) { if (generation === navGeneration) { location.hash = ''; await showFailureAlert(error); } return; }
  if (generation !== navGeneration) return;
  if (!loaded.some(deck => deck.id === id)) {
    location.hash = ''; await showAlert({title:t('deckNotFound'),bodyHtml:'<p>' + esc(t('deckUnavailable')) + '</p>'}); return;
  }
  const resolved = await loadRailScope(id);
  if (generation !== navGeneration) return;
  decks = loaded; decksLoaded = true; decksLoadFailed = false; railScopeIds = resolved.ids;
  if (resolved.data) { libraryData = resolved.data; updateProjectNav(); }
  showViewerScreen(); void recordRecent(id); await selectDeck(id);
}
function route() {
  const generation = ++navGeneration, id = location.hash.slice(1);
  closeMenu();
  if (!id) showLibraryScreen(); else void openDeckFromHash(id,generation);
}
window.addEventListener('hashchange',route);
route();
