// Kroger Reorder page logic. Vanilla JS, talks to the JSON API in kroger_reorder/main.py.
const $ = (id) => document.getElementById(id);
const LOCAL_HOST = ['localhost', '127.0.0.1'].includes(location.hostname);
function isHost() { return LOCAL_HOST || (!!state.public_url && location.origin === String(state.public_url).toLowerCase()); }
const STALE_MS = 24 * 60 * 60 * 1000;

let state = { store: null, signed_in: false, lists: [], active_list: null, name: '', items: [], warning: null,
              photo_backend: null, public_url: null };
let importRows = [];  // [{raw, name, quantity, candidates, kind?, reason?, group?}]  (kind/reason/group only from Photo and Recipe)
let orders = [];        // from GET /api/orders, newest first
let openOrderId = null; // which order row is expanded

// -- helpers ----------------------------------------------------------------------------

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (data.signed_in === false) { state.signed_in = false; render(); }
    throw Object.assign(new Error(data.error || `HTTP ${res.status}`), { data });
  }
  return data;
}

function banner(kind, text, html = false, el = $('banner')) {
  el.className = 'banner' + (kind ? ' ' + kind : '');
  if (html) el.innerHTML = text || ''; else el.textContent = text || '';
}

function money(n) { return `$${Number(n).toFixed(2)}`; }

function priceHtml(p) {
  if (p.promo != null && p.price != null && Number(p.promo) < Number(p.price)) {
    return `<span class="sale">${money(p.promo)}<s>${money(p.price)}</s></span>`;
  }
  if (p.promo != null && p.price == null) return `<span class="sale">${money(p.promo)}</span>`;
  if (p.price != null) return `<span class="price">${money(p.price)}</span>`;
  return '';
}

function thumb(url) {
  return url ? `<img class="thumb" src="${esc(url)}" alt="" loading="lazy">` : `<div class="thumb placeholder"></div>`;
}

function listPath(suffix = '') {
  return `/api/lists/${encodeURIComponent(state.active_list)}${suffix}`;
}

function cleanItems() {
  return state.items.map(({ upc, name, size, quantity, included, image, price, promo, price_checked_at }) =>
    ({ upc, name, size, quantity, included, image, price, promo, price_checked_at }));
}

function ago(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const h = Math.floor(ms / 3600000);
  if (h < 1) return 'just now';
  if (h < 24) return `${h} hour${h === 1 ? '' : 's'} ago`;
  const d = Math.floor(h / 24);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}

function newestCheck() {
  const stamps = state.items.map(i => i.price_checked_at).filter(Boolean).sort();
  return stamps.length ? stamps[stamps.length - 1] : null;
}

function pricesStale() {
  if (!state.store || !state.items.length) return false;
  const newest = newestCheck();
  return !newest || (Date.now() - new Date(newest).getTime()) > STALE_MS;
}

// -- rendering ------------------------------------------------------------------------

function render() {
  const setupDone = !!(state.store && state.signed_in);
  $('setup').classList.toggle('hidden', setupDone);
  $('store-block').classList.toggle('hidden', !!state.store);
  $('signin-block').classList.toggle('hidden', !!state.signed_in);
  $('signin-link').classList.toggle('hidden', !isHost());
  $('signin-remote').classList.toggle('hidden', isHost());
  $('add').classList.toggle('hidden', !state.store);
  $('store-label').textContent = state.store ? state.store.store_name : '';
  $('push').disabled = !setupDone || !state.items.some(i => i.included);
  $('check-prices').disabled = !state.store || !state.items.length;

  const sel = $('list-select');
  sel.innerHTML = '';
  state.lists.forEach(l => {
    const opt = document.createElement('option');
    opt.value = l.slug;
    opt.textContent = `${l.name} (${l.count})`;
    opt.selected = l.slug === state.active_list;
    sel.appendChild(opt);
  });

  const newest = newestCheck();
  $('price-status').textContent = !state.items.length ? '' : newest ? `Prices checked ${ago(newest)}` : 'Prices not checked yet';

  const ul = $('items');
  const active = document.activeElement?.dataset || {};
  ul.innerHTML = '';
  state.items.forEach((item, idx) => {
    const li = document.createElement('li');
    if (item.bad) li.classList.add('bad');
    li.innerHTML = `
      <input type="checkbox" ${item.included ? 'checked' : ''} data-idx="${idx}" data-field="included">
      ${thumb(item.image)}
      <div class="name"><div>${esc(item.name)}</div><div class="size">${esc(item.size)} ${priceHtml(item)}</div></div>
      <input type="number" min="1" value="${item.quantity}" data-idx="${idx}" data-field="quantity">
      <button class="danger" data-remove="${esc(item.upc)}" title="Remove">&#x2715;</button>`;
    ul.appendChild(li);
  });
  if (active.idx !== undefined) {
    ul.querySelector(`[data-idx="${active.idx}"][data-field="${active.field}"]`)?.focus();
  }
  $('empty').classList.toggle('hidden', state.items.length > 0);
}

function adoptList(data) {
  const entry = state.lists.find(l => l.slug === data.slug);
  if (entry) { entry.name = data.name; entry.count = data.items.length; }
  if (data.slug !== state.active_list) return;   // stale response for another list
  const bad = new Set(state.items.filter(i => i.bad).map(i => i.upc));
  state.items = data.items.map(i => ({ ...i, bad: bad.has(i.upc) }));
  state.name = data.name;
  state.warning = data.warning;
}

// -- loading ----------------------------------------------------------------------------

async function refreshLists() {
  const s = await api('GET', '/api/state');
  Object.assign(state, { store: s.store, signed_in: s.signed_in, lists: s.lists, active_list: s.active_list,
                         photo_backend: s.photo_backend, public_url: s.public_url });
}

async function loadList() {
  if (!state.active_list) { state.items = []; state.name = ''; return; }
  adoptList(await api('GET', listPath()));
  if (state.warning) banner('warn', state.warning);
}

async function refreshPrices(manual) {
  if (!state.store || !state.items.length) return;
  const slug = state.active_list;
  $('check-prices').disabled = true;
  $('price-status').textContent = 'Checking prices…';
  try {
    const data = await api('POST', listPath('/prices'));
    adoptList(data);
    if (manual && slug === state.active_list) {
      banner('ok', data.failed ? `Prices updated; ${data.failed} item${data.failed === 1 ? '' : 's'} couldn't be checked.` : 'Prices updated.');
    }
  } catch (e) {
    if (manual) banner('error', e.message);
  }
  render();
}

async function load() {
  const params = new URLSearchParams(location.search);
  const authError = params.get('auth_error');
  if (authError) history.replaceState(null, '', location.pathname + location.hash);
  showTab(tabFromHash());
  try {
    await refreshLists();
    await loadList();
    render();
    if (authError) banner('error', `Kroger sign-in failed: ${authError}`);
    if (pricesStale()) refreshPrices(false);
  } catch (e) { banner('error', e.message); render(); }
}

async function switchList(slug) {
  await api('PUT', '/api/active', { slug });
  state.active_list = slug;
  banner();
  await loadList();
  render();
  if (pricesStale()) refreshPrices(false);
}

async function saveItems() {
  try { adoptList(await api('PUT', listPath('/items'), cleanItems())); }
  catch (e) { banner('error', e.message); }
}

// -- setup ------------------------------------------------------------------------------

$('find-stores').onclick = async () => {
  banner();
  try {
    const { stores } = await api('GET', `/api/stores?zip=${encodeURIComponent($('zip').value.trim())}`);
    const ul = $('store-results'); ul.innerHTML = '';
    stores.forEach(s => {
      const li = document.createElement('li');
      li.innerHTML = `<div class="name"><div>${esc(s.name)}</div><div class="size">${esc(s.address)}</div></div>`;
      li.onclick = async () => {
        try {
          const data = await api('PUT', '/api/store', { id: s.id, name: s.name });
          state.store = data.store; ul.innerHTML = ''; render();
          if (pricesStale()) refreshPrices(false);
        } catch (e) { banner('error', e.message); }
      };
      ul.appendChild(li);
    });
    if (!stores.length) banner('warn', 'No stores found near that zip.');
  } catch (e) { banner('error', e.message); }
};

$('change-store').onclick = () => { state.store = null; render(); };

// -- search / add -----------------------------------------------------------------------

async function search() {
  banner();
  const term = $('term').value.trim();
  if (!term) return;
  try {
    const { results } = await api('GET', `/api/search?term=${encodeURIComponent(term)}`);
    const ul = $('search-results'); ul.innerHTML = '';
    results.forEach(r => {
      const li = document.createElement('li');
      li.innerHTML = `${thumb(r.image)}<div class="name"><div>${esc(r.name)}</div><div class="size">${esc(r.size)}</div></div>${priceHtml(r)}`;
      li.onclick = async () => {
        try {
          const data = await api('POST', listPath('/items'),
            { upc: r.upc, name: r.name, size: r.size, image: r.image, price: r.price, promo: r.promo });
          adoptList(data); ul.innerHTML = ''; $('term').value = ''; render();
        } catch (e) { banner('error', e.message); }
      };
      ul.appendChild(li);
    });
    if (!results.length) banner('warn', `No matches for "${term}" at your store.`);
  } catch (e) { banner('error', e.message); }
}
$('search').onclick = search;
$('term').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });

// -- list rows ----------------------------------------------------------------------------

$('items').addEventListener('change', async (e) => {
  const { idx, field } = e.target.dataset;
  if (idx === undefined) return;
  const item = state.items[idx];
  if (field === 'included') item.included = e.target.checked;
  if (field === 'quantity') item.quantity = Math.max(1, parseInt(e.target.value || '1', 10) || 1);
  await saveItems();
  render();
});

$('items').addEventListener('click', async (e) => {
  const upc = e.target.dataset.remove;
  if (!upc) return;
  try { adoptList(await api('DELETE', listPath(`/items/${encodeURIComponent(upc)}`))); render(); }
  catch (err) { banner('error', err.message); }
});

$('check-prices').onclick = () => { banner(); refreshPrices(true); };

// -- list selector and menu --------------------------------------------------------------

$('list-select').onchange = (e) => switchList(e.target.value).catch(err => { banner('error', err.message); render(); });

$('menu-btn').onclick = (e) => { e.stopPropagation(); $('menu').classList.toggle('hidden'); };
document.addEventListener('click', () => $('menu').classList.add('hidden'));

// One handler for every list action: the +, pencil, Import, and Export buttons, and the ⋯ menu.
$('list-section').addEventListener('click', async (e) => {
  const action = e.target.closest('[data-action]')?.dataset.action;  // closest: the icon SVG may be the target
  if (!action) return;
  $('menu').classList.add('hidden');
  banner();
  try {
    if (action === 'new') {
      const name = prompt('Name for the new list:');
      if (!name?.trim()) return;
      const { slug } = await api('POST', '/api/lists', { name });
      await refreshLists();
      await switchList(slug);
    } else if (action === 'rename') {
      const name = prompt('New name for this list:', state.name);
      if (!name?.trim() || name === state.name) return;
      await api('PATCH', listPath(), { name });
      await refreshLists(); await loadList(); render();
    } else if (action === 'duplicate') {
      const name = prompt('Name for the copy:', `${state.name} copy`);
      if (!name?.trim()) return;
      const { slug } = await api('POST', listPath('/duplicate'), { name });
      await refreshLists();
      await switchList(slug);
    } else if (action === 'delete') {
      if (!confirm(`Delete "${state.name}" and its ${state.items.length} item${state.items.length === 1 ? '' : 's'}?`)) return;
      const data = await api('DELETE', listPath());
      state.lists = data.lists;
      await switchList(data.active_list);
    } else if (action === 'import') {
      openImport();
    } else if (action === 'photo') {
      openPhoto();
    } else if (action === 'recipe') {
      openRecipe();
    } else if (action === 'export') {
      const a = document.createElement('a');
      a.href = listPath('/export.csv');
      a.download = `${state.active_list}.csv`;
      document.body.appendChild(a); a.click(); a.remove();
    }
  } catch (err) { banner('error', err.message); }
});

// -- import dialog --------------------------------------------------------------------------

function openImport() {
  importRows = [];
  $('import-text').value = '';
  $('import-file').value = '';
  $('import-review').classList.add('hidden');
  $('import-table').querySelector('tbody').innerHTML = '';
  banner(null, '', false, $('import-banner'));
  $('import-dialog').showModal();
}

$('import-close').onclick = () => $('import-dialog').close();

$('import-file').onchange = async (e) => {
  const file = e.target.files[0];
  if (file) $('import-text').value = await file.text();
};

$('import-text').oninput = () => { importRows = []; $('import-review').classList.add('hidden'); };

// Match parsed lines against the store catalog and show the review table.
// Rows with a UPC (an exported CSV) are exact products; only the rest need a catalog search.
async function reviewLines(lines, bannerEl) {
  const toResolve = lines.filter(l => !l.exact);
  let matches = [];
  if (toResolve.length) {
    banner('warn', `Looking up ${plural(toResolve.length, 'item')} at your store…`, false, bannerEl);
    ({ matches } = await api('POST', '/api/import/resolve', { names: toResolve.map(l => l.name) }));
  }
  let next = 0;
  importRows = groupTogether(lines.map(l => l.exact
    ? { ...l, candidates: [{ ...l.exact, name: l.name, fromFile: true }] }
    : { ...l, candidates: matches[next++].candidates }));
  banner(null, '', false, bannerEl);
  renderImport();
}

// Rows that share a group (Claude's alternatives for one need, e.g. "Fresh fruit") sit together, in
// order of first appearance; everything else keeps its order.
function groupTogether(rows) {
  const out = [], placed = new Set();
  rows.forEach((row, i) => {
    if (placed.has(i)) return;
    out.push(row); placed.add(i);
    if (row.group) rows.forEach((other, j) => {
      if (!placed.has(j) && other.group === row.group) { out.push(other); placed.add(j); }
    });
  });
  return out;
}

$('import-parse').onclick = async () => {
  $('import-parse').disabled = true;
  banner(null, '', false, $('import-banner'));
  const text = $('import-text').value;
  try {
    const { lines } = await api('POST', '/api/import/parse', { text });
    await reviewLines(lines, $('import-banner'));
  } catch (e) {
    banner('error', e.message, false, $('import-banner'));
  } finally {
    $('import-parse').disabled = false;
  }
};

const SEARCH_PICK = '-2';  // the select value that turns a row into a catalog search box

function renderRow(idx) {
  const row = importRows[idx];
  const tr = document.createElement('tr');
  tr.dataset.importRow = idx;
  // Rows the list already covers, pantry staples, and stocked items start on Skip: shown for
  // transparency, not bought by default; one pick adds them anyway.
  const skip = !row.searched && ['staple', 'on_list', 'stocked'].includes(row.kind);
  const options = row.candidates.map((c, ci) =>
    `<option value="${ci}" ${ci === 0 && !skip ? 'selected' : ''}>${c.fromFile ? 'From file: ' : ''}${esc(c.name)}${c.size ? ' · ' + esc(c.size) : ''}${c.price != null || c.promo != null ? ' · ' + money(c.promo ?? c.price) : ''}</option>`
  ).join('');
  const kind = row.kind ? `<span class="kind ${esc(row.kind)}">${esc(row.kind.replace('_', ' '))}</span>` : '';
  const sub = row.kind ? (row.reason || '') : row.raw;
  tr.innerHTML = `
    <td><div>${kind}${esc(row.name)}</div><div class="raw">${esc(sub)}</div></td>
    <td><input type="number" min="1" value="${row.quantity}" data-import-qty="${idx}"></td>
    <td><select data-import-pick="${idx}">${options}<option value="-1" ${row.candidates.length && !skip ? '' : 'selected'}>${row.candidates.length ? 'Skip this line' : 'No match found — skip'}</option><option value="${SEARCH_PICK}">Search for a different item…</option></select></td>`;
  return tr;
}

function renderImport() {
  const tbody = $('import-table').querySelector('tbody');
  tbody.innerHTML = '';
  let lastGroup = null;
  importRows.forEach((row, idx) => {
    if (row.group && row.group !== lastGroup) {
      const heading = document.createElement('tr');
      heading.className = 'group';
      heading.innerHTML = `<td colspan="3">${esc(row.group)}</td>`;
      tbody.appendChild(heading);
    }
    lastGroup = row.group || null;
    tbody.appendChild(renderRow(idx));
  });
  $('import-review').classList.remove('hidden');
  updateImportSummary();
}

// The quantity lives only in the row's input until the row is re-rendered; keep edits across a search.
function rememberQty(idx) {
  const input = $('import-table').querySelector(`[data-import-qty="${idx}"]`);
  if (input) importRows[idx].quantity = Math.max(1, parseInt(input.value, 10) || 1);
}

// "Search for a different item…": the match cell becomes a search box for this row only.
function startRowSearch(idx) {
  const cell = $('import-table').querySelector(`[data-import-pick="${idx}"]`).parentElement;
  cell.innerHTML = `<div class="row"><input type="text" data-import-search="${idx}" placeholder="Search your store" value="${esc(importRows[idx].name)}"><button class="secondary" data-import-search-go="${idx}">Search</button><button class="secondary" data-import-search-cancel="${idx}">Cancel</button></div>`;
  cell.querySelector('input').focus();
  updateImportSummary();
}

function cancelRowSearch(idx) {
  rememberQty(idx);
  $('import-table').querySelector(`[data-import-row="${idx}"]`).replaceWith(renderRow(idx));
  updateImportSummary();
}

async function runRowSearch(idx) {
  const input = $('import-table').querySelector(`[data-import-search="${idx}"]`);
  const term = input.value.trim();
  if (!term) return;
  const button = $('import-table').querySelector(`[data-import-search-go="${idx}"]`);
  input.disabled = button.disabled = true;
  try {
    const { matches } = await api('POST', '/api/import/resolve', { names: [term] });
    const row = importRows[idx];
    rememberQty(idx);
    row.name = term;
    row.candidates = matches[0].candidates;
    row.searched = true;  // the user chose this match on purpose, so it starts on add whatever the kind
    $('import-table').querySelector(`[data-import-row="${idx}"]`).replaceWith(renderRow(idx));
    banner(row.candidates.length ? null : 'warn', row.candidates.length ? '' : `No match for “${term}” at your store.`, false, $('import-banner'));
    updateImportSummary();
  } catch (e) {
    input.disabled = button.disabled = false;
    banner('error', e.message, false, $('import-banner'));
  }
}

function importPicks() {
  return importRows.map((row, idx) => {
    const select = $('import-table').querySelector(`[data-import-pick="${idx}"]`);
    if (!select) return null;  // the row is in search mode
    const pick = parseInt(select.value, 10);
    const qty = Math.max(1, parseInt($('import-table').querySelector(`[data-import-qty="${idx}"]`).value || '1', 10) || 1);
    if (pick < 0) return null;
    const c = row.candidates[pick];
    const item = { upc: c.upc, name: c.name, size: c.size, image: c.image, quantity: qty };
    if (c.fromFile) Object.assign(item, { included: c.included, price: c.price, promo: c.promo });
    return item;
  }).filter(Boolean);
}

function updateImportSummary() {
  const n = importPicks().length;
  $('import-commit').disabled = n === 0;
  $('import-summary').textContent = `${n} of ${importRows.length} line${importRows.length === 1 ? '' : 's'} will be added`;
}

$('import-table').addEventListener('change', (e) => {
  const idx = e.target.dataset.importPick;
  if (idx !== undefined && e.target.value === SEARCH_PICK) startRowSearch(parseInt(idx, 10));
  else updateImportSummary();
});
$('import-table').addEventListener('click', (e) => {
  const go = e.target.dataset.importSearchGo, cancel = e.target.dataset.importSearchCancel;
  if (go !== undefined) runRowSearch(parseInt(go, 10));
  else if (cancel !== undefined) cancelRowSearch(parseInt(cancel, 10));
});
$('import-table').addEventListener('keydown', (e) => {
  const idx = e.target.dataset.importSearch;
  if (idx === undefined) return;
  if (e.key === 'Enter') { e.preventDefault(); runRowSearch(parseInt(idx, 10)); }
  else if (e.key === 'Escape') { e.preventDefault(); cancelRowSearch(parseInt(idx, 10)); }
});

$('import-commit').onclick = async () => {
  $('import-commit').disabled = true;
  const picks = importPicks();
  try {
    adoptList(await api('POST', listPath('/import'), picks));
    $('import-dialog').close();
    render();
    banner('ok', `Added ${picks.length} item${picks.length === 1 ? '' : 's'} to ${state.name}.`);
    if (pricesStale()) refreshPrices(false);
  } catch (e) {
    banner('error', e.message, false, $('import-banner'));
  } finally {
    $('import-commit').disabled = false;
  }
};

// -- photo suggestions ------------------------------------------------------------------------

const PHOTO_MAX = 4;
const PHOTO_EDGE = 1600;   // longest side after downscaling, keeps uploads small and under Claude's limits
let photoFiles = [];       // [{media_type, data, url}] ready to send

// The "Add to" picker shared by the Photo and Recipe dialogs: every list plus "New list…".
function fillListPicker(sel) {
  sel.innerHTML = '';
  state.lists.forEach(l => {
    const opt = document.createElement('option');
    opt.value = l.slug; opt.textContent = l.name; opt.selected = l.slug === state.active_list;
    sel.appendChild(opt);
  });
  const fresh = document.createElement('option');
  fresh.value = '__new__'; fresh.textContent = 'New list…';
  sel.appendChild(fresh);
}

function backendText(what) {
  return {
    api: `${what} analyzed through the Anthropic API key in .env.`,
    cli: `${what} analyzed by Claude Code on the computer running the app.`,
  }[state.photo_backend] || 'Claude isn\'t set up yet: add ANTHROPIC_API_KEY to .env, or install Claude Code on the computer running the app.';
}

// Resolve the picker's choice to a slug, creating the list when "New list…" was chosen. null = cancelled.
async function pickTargetList(sel) {
  let slug = sel.value;
  if (slug === '__new__') {
    const name = prompt('Name for the new list:');
    if (!name?.trim()) return null;
    ({ slug } = await api('POST', '/api/lists', { name }));
    await refreshLists();
  }
  return slug;
}

function openPhoto() {
  photoFiles = [];
  $('photo-files').value = '';
  $('photo-note').value = '';
  $('photo-thumbs').innerHTML = '';
  $('photo-go').disabled = true;
  banner(null, '', false, $('photo-banner'));
  $('photo-backend').textContent = backendText('Photos are');
  fillListPicker($('photo-list'));
  $('photo-dialog').showModal();
}

$('photo-close').onclick = () => $('photo-dialog').close();

// Shrink a photo in the browser: phone pictures are 3-12 MB; 1600px JPEG is plenty for Claude.
function shrinkPhoto(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, PHOTO_EDGE / Math.max(img.width, img.height));
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
      resolve({ media_type: 'image/jpeg', data: dataUrl.split(',')[1], url: dataUrl });
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error(`Couldn't read ${file.name}.`)); };
    img.src = url;
  });
}

$('photo-files').onchange = async (e) => {
  const files = Array.from(e.target.files || []).slice(0, PHOTO_MAX);
  banner(null, '', false, $('photo-banner'));
  if ((e.target.files || []).length > PHOTO_MAX) banner('warn', `Only the first ${PHOTO_MAX} photos will be used.`, false, $('photo-banner'));
  try {
    photoFiles = await Promise.all(files.map(shrinkPhoto));
    $('photo-thumbs').innerHTML = photoFiles.map(p => `<img src="${p.url}" alt="">`).join('');
    $('photo-go').disabled = photoFiles.length === 0;
  } catch (err) {
    photoFiles = [];
    $('photo-thumbs').innerHTML = '';
    $('photo-go').disabled = true;
    banner('error', err.message, false, $('photo-banner'));
  }
};

$('photo-go').onclick = async () => {
  const pbanner = $('photo-banner');
  if (!photoFiles.length) return;
  $('photo-go').disabled = true;
  try {
    // Decide where the items will go first, so the review lands on that list.
    const slug = await pickTargetList($('photo-list'));
    if (!slug) return;
    banner('warn', 'Claude is looking at your photos… this takes 10 to 30 seconds.', false, pbanner);
    const { lines } = await api('POST', '/api/photo/suggest', {
      photos: photoFiles.map(({ media_type, data }) => ({ media_type, data })),
      slug,
      note: $('photo-note').value,
    });
    if (slug !== state.active_list) await switchList(slug);
    $('photo-dialog').close();
    openImport();
    banner('warn', `Claude suggested ${plural(lines.length, 'item')}. Matching them to your store…`, false, $('import-banner'));
    await reviewLines(lines, $('import-banner'));
  } catch (err) {
    banner('error', err.message, false, $('photo-dialog').open ? pbanner : $('import-banner'));
  } finally {
    $('photo-go').disabled = photoFiles.length === 0;
  }
};

// -- recipe suggestions ------------------------------------------------------------------------

function openRecipe() {
  $('recipe-url').value = '';
  $('recipe-note').value = '';
  $('recipe-go').disabled = true;
  banner(null, '', false, $('recipe-banner'));
  $('recipe-backend').textContent = backendText('Recipes are');
  fillListPicker($('recipe-list'));
  $('recipe-dialog').showModal();
  $('recipe-url').focus();
}

$('recipe-close').onclick = () => $('recipe-dialog').close();
$('recipe-url').oninput = () => { $('recipe-go').disabled = !$('recipe-url').value.trim(); };
$('recipe-url').onkeydown = (e) => { if (e.key === 'Enter' && !$('recipe-go').disabled) { e.preventDefault(); $('recipe-go').click(); } };

$('recipe-go').onclick = async () => {
  const rbanner = $('recipe-banner');
  const url = $('recipe-url').value.trim();
  if (!url) return;
  $('recipe-go').disabled = true;
  try {
    const slug = await pickTargetList($('recipe-list'));
    if (!slug) return;
    banner('warn', 'Fetching the recipe and asking Claude… this takes 10 to 30 seconds.', false, rbanner);
    const { title, lines } = await api('POST', '/api/recipe/suggest', { url, slug, note: $('recipe-note').value });
    if (slug !== state.active_list) await switchList(slug);
    $('recipe-dialog').close();
    openImport();
    banner('warn', `Claude suggested ${plural(lines.length, 'item')} for “${title || 'the recipe'}”. Matching them to your store…`, false, $('import-banner'));
    await reviewLines(lines, $('import-banner'));
  } catch (err) {
    banner('error', err.message, false, $('recipe-dialog').open ? rbanner : $('import-banner'));
  } finally {
    $('recipe-go').disabled = !$('recipe-url').value.trim();
  }
};

// -- usage profile ----------------------------------------------------------------------------

function renderUsage(items) {
  const tbody = $('usage-table').querySelector('tbody');
  tbody.innerHTML = '';
  items.forEach(r => {
    const tr = document.createElement('tr');
    const last = r.days_since_last == null ? '' : r.days_since_last === 0 ? 'today' : `${plural(r.days_since_last, 'day')} ago`;
    const every = r.avg_days_between == null ? '' : `${Math.round(r.avg_days_between)} days`;
    tr.innerHTML = `<td>${esc(r.name || r.upc)}</td><td>${r.times}×</td><td>${r.avg_quantity}</td><td>${last}</td><td>${every}</td>`;
    tbody.appendChild(tr);
  });
  $('usage-table').classList.toggle('hidden', items.length === 0);
  $('usage-empty').classList.toggle('hidden', items.length > 0);
}

async function loadUsage() {
  renderUsage((await api('GET', '/api/usage')).items);
}

// -- orders dialog ---------------------------------------------------------------------------

function whenText(iso) {
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function previewText(items) {
  const names = items.slice(0, 3).map(i => esc(i.name));
  const more = items.length - names.length;
  return names.join(', ') + (more > 0 ? ` +${more} more` : '');
}

function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

function renderOrders() {
  const ul = $('orders'); ul.innerHTML = '';
  orders.forEach(o => {
    const li = document.createElement('li');
    li.dataset.id = o.id;
    const total = o.estimated_total != null ? ` · est. ${money(o.estimated_total)}` : '';
    const tag = o.reorder_of ? '<span class="tag">reorder</span>' : '';
    const details = o.id !== openOrderId ? '' : `
      <div class="details">
        <ul>${o.items.map(i => `<li>${thumb(i.image)}
          <div class="name"><div>${esc(i.name)}</div><div class="size">${esc(i.size)} ${priceHtml(i)}</div></div>
          <span class="qty">&times;${i.quantity}</span></li>`).join('')}</ul>
        <div class="row" style="margin-top:10px">
          <button data-reorder="${esc(o.id)}">Send to cart again</button>
          <button class="danger" data-delete-order="${esc(o.id)}">Delete</button>
        </div>
      </div>`;
    li.innerHTML = `
      <div class="head"><div class="name">
        <div>${esc(whenText(o.placed_at))} · ${esc(o.list_name || 'List')}${tag}</div>
        <div class="size">${plural(o.item_count, 'item')}${total} · ${previewText(o.items)}</div>
      </div></div>${details}`;
    ul.appendChild(li);
  });
  $('orders-empty').classList.toggle('hidden', orders.length > 0);
}

async function loadOrders() {
  orders = (await api('GET', '/api/orders')).orders;
  renderOrders();
}

// -- tabs ---------------------------------------------------------------------------------------

function showTab(name) {
  const onOrders = name === 'orders';
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $('tab-list').classList.toggle('hidden', onOrders);
  $('tab-orders').classList.toggle('hidden', !onOrders);
  $('footer').classList.toggle('hidden', onOrders);
  const hash = onOrders ? '#orders' : '';
  if (location.hash !== hash) history.replaceState(null, '', location.pathname + location.search + hash);
  if (onOrders) {
    banner(null, '', false, $('orders-banner'));
    loadOrders().catch(e => banner('error', e.message, false, $('orders-banner')));
    loadUsage().catch(e => banner('error', e.message, false, $('orders-banner')));
  }
}

function tabFromHash() { return location.hash === '#orders' ? 'orders' : 'list'; }

document.querySelectorAll('.tab').forEach(t => { t.onclick = () => showTab(t.dataset.tab); });
window.addEventListener('hashchange', () => showTab(tabFromHash()));

$('orders').addEventListener('click', async (e) => {
  const obanner = $('orders-banner');
  const reorderId = e.target.dataset.reorder;
  const deleteId = e.target.dataset.deleteOrder;
  if (reorderId) {
    e.target.disabled = true;
    banner(null, '', false, obanner);
    const source = orders.find(o => o.id === reorderId);
    try {
      const { order, warning } = await api('POST', `/api/orders/${encodeURIComponent(reorderId)}/reorder`);
      await loadOrders();
      const done = `Sent ${plural(order ? order.item_count : source.item_count, 'item')} to your Kroger cart. ` +
        `Finish checkout at <a href="https://www.kroger.com/cart" target="_blank" rel="noopener">kroger.com/cart</a> or in the Kroger app.`;
      banner(warning ? 'warn' : 'ok', warning ? `${esc(warning)} ${done}` : done, true, obanner);
    } catch (err) {
      const bad = (err.data && err.data.bad_upcs) || [];
      const names = bad.map(u => source?.items.find(i => i.upc === u)?.name || u);
      banner('error', err.message + (names.length ? ` Rejected: ${names.join(', ')}.` : ''), false, obanner);
      e.target.disabled = false;
    }
    return;
  }
  if (deleteId) {
    if (!confirm('Delete this order from your history?')) return;
    try {
      await api('DELETE', `/api/orders/${encodeURIComponent(deleteId)}`);
      orders = orders.filter(o => o.id !== deleteId);
      if (openOrderId === deleteId) openOrderId = null;
      renderOrders();
    } catch (err) { banner('error', err.message, false, obanner); }
    return;
  }
  if (e.target.closest('.details')) return;  // clicks inside the item list don't collapse it
  const li = e.target.closest('li[data-id]');
  if (!li) return;
  openOrderId = openOrderId === li.dataset.id ? null : li.dataset.id;
  renderOrders();
});

// -- push -------------------------------------------------------------------------------------

$('push').onclick = async () => {
  banner();
  $('push').disabled = true;
  try {
    const { added, warning } = await api('POST', listPath('/push'), cleanItems());
    state.items.forEach(i => delete i.bad);
    const done = `Added to your Kroger cart: ${added.map(i => esc(i.name)).join(', ')}. ` +
      `Finish checkout at <a href="https://www.kroger.com/cart" target="_blank" rel="noopener">kroger.com/cart</a> or in the Kroger app.`;
    banner(warning ? 'warn' : 'ok', warning ? `${done} ${esc(warning)}` : done, true);
  } catch (e) {
    const bad = (e.data && e.data.bad_upcs) || [];
    state.items.forEach(i => { i.bad = bad.includes(i.upc); });
    banner('error', e.message + (bad.length ? ' The highlighted item was rejected; remove or replace it.' : ''));
  }
  render();
};

load();
