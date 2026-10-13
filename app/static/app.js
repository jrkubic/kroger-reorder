// Kroger Reorder page logic. Vanilla JS, talks to the JSON API in app/main.py.
const $ = (id) => document.getElementById(id);
const IS_HOST = ['localhost', '127.0.0.1'].includes(location.hostname);
const STALE_MS = 24 * 60 * 60 * 1000;

let state = { store: null, signed_in: false, lists: [], active_list: null, name: '', items: [], warning: null };
let importRows = [];  // [{raw, name, quantity, candidates}]

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
  $('signin-link').classList.toggle('hidden', !IS_HOST);
  $('signin-remote').classList.toggle('hidden', IS_HOST);
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
  const bad = new Set(state.items.filter(i => i.bad).map(i => i.upc));
  state.items = data.items.map(i => ({ ...i, bad: bad.has(i.upc) }));
  state.name = data.name;
  state.warning = data.warning;
  const entry = state.lists.find(l => l.slug === data.slug);
  if (entry) { entry.name = data.name; entry.count = data.items.length; }
}

// -- loading ----------------------------------------------------------------------------

async function refreshLists() {
  const s = await api('GET', '/api/state');
  Object.assign(state, { store: s.store, signed_in: s.signed_in, lists: s.lists, active_list: s.active_list });
}

async function loadList() {
  if (!state.active_list) { state.items = []; state.name = ''; return; }
  adoptList(await api('GET', listPath()));
  if (state.warning) banner('warn', state.warning);
}

async function refreshPrices(manual) {
  if (!state.store || !state.items.length) return;
  $('check-prices').disabled = true;
  $('price-status').textContent = 'Checking prices…';
  try {
    const data = await api('POST', listPath('/prices'));
    adoptList(data);
    if (manual) banner('ok', data.failed ? `Prices updated; ${data.failed} item${data.failed === 1 ? '' : 's'} couldn't be checked.` : 'Prices updated.');
  } catch (e) {
    if (manual) banner('error', e.message);
  }
  render();
}

async function load() {
  const params = new URLSearchParams(location.search);
  const authError = params.get('auth_error');
  if (authError) history.replaceState(null, '', location.pathname);
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

$('list-select').onchange = (e) => switchList(e.target.value).catch(err => banner('error', err.message));

$('menu-btn').onclick = (e) => { e.stopPropagation(); $('menu').classList.toggle('hidden'); };
document.addEventListener('click', () => $('menu').classList.add('hidden'));

$('menu').addEventListener('click', async (e) => {
  const action = e.target.dataset.action;
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
      state.lists = data.lists; state.active_list = data.active_list;
      await loadList(); render();
    } else if (action === 'import') {
      openImport();
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

$('import-parse').onclick = async () => {
  banner(null, '', false, $('import-banner'));
  const text = $('import-text').value;
  try {
    const { lines } = await api('POST', '/api/import/parse', { text });
    $('import-parse').disabled = true;
    banner('warn', `Looking up ${lines.length} item${lines.length === 1 ? '' : 's'} at your store…`, false, $('import-banner'));
    const { matches } = await api('POST', '/api/import/resolve', { names: lines.map(l => l.name) });
    importRows = lines.map((l, i) => ({ ...l, candidates: matches[i].candidates }));
    banner(null, '', false, $('import-banner'));
    renderImport();
  } catch (e) {
    banner('error', e.message, false, $('import-banner'));
  }
  $('import-parse').disabled = false;
};

function renderImport() {
  const tbody = $('import-table').querySelector('tbody');
  tbody.innerHTML = '';
  importRows.forEach((row, idx) => {
    const tr = document.createElement('tr');
    const options = row.candidates.map((c, ci) =>
      `<option value="${ci}" ${ci === 0 ? 'selected' : ''}>${esc(c.name)} · ${esc(c.size)}${c.price != null ? ' · ' + money(c.promo ?? c.price) : ''}</option>`
    ).join('');
    tr.innerHTML = `
      <td><div>${esc(row.name)}</div><div class="raw">${esc(row.raw)}</div></td>
      <td><input type="number" min="1" value="${row.quantity}" data-import-qty="${idx}"></td>
      <td><select data-import-pick="${idx}">${options}<option value="-1" ${row.candidates.length ? '' : 'selected'}>${row.candidates.length ? 'Skip this line' : 'No match found — skip'}</option></select></td>`;
    tbody.appendChild(tr);
  });
  $('import-review').classList.remove('hidden');
  updateImportSummary();
}

function importPicks() {
  return importRows.map((row, idx) => {
    const pick = parseInt($('import-table').querySelector(`[data-import-pick="${idx}"]`).value, 10);
    const qty = Math.max(1, parseInt($('import-table').querySelector(`[data-import-qty="${idx}"]`).value || '1', 10) || 1);
    if (pick < 0) return null;
    const c = row.candidates[pick];
    return { upc: c.upc, name: c.name, size: c.size, image: c.image, quantity: qty };
  }).filter(Boolean);
}

function updateImportSummary() {
  const n = importPicks().length;
  $('import-commit').disabled = n === 0;
  $('import-summary').textContent = `${n} of ${importRows.length} line${importRows.length === 1 ? '' : 's'} will be added`;
}

$('import-table').addEventListener('change', updateImportSummary);

$('import-commit').onclick = async () => {
  const picks = importPicks();
  try {
    adoptList(await api('POST', listPath('/import'), picks));
    $('import-dialog').close();
    render();
    banner('ok', `Added ${picks.length} item${picks.length === 1 ? '' : 's'} to ${state.name}.`);
    if (pricesStale()) refreshPrices(false);
  } catch (e) {
    banner('error', e.message, false, $('import-banner'));
  }
};

// -- push -------------------------------------------------------------------------------------

$('push').onclick = async () => {
  banner();
  $('push').disabled = true;
  try {
    const { added } = await api('POST', listPath('/push'), cleanItems());
    state.items.forEach(i => delete i.bad);
    banner('ok', `Added to your Kroger cart: ${added.map(i => esc(i.name)).join(', ')}. ` +
      `Finish checkout at <a href="https://www.kroger.com/cart" target="_blank" rel="noopener">kroger.com/cart</a> or in the Kroger app.`, true);
  } catch (e) {
    const bad = (e.data && e.data.bad_upcs) || [];
    state.items.forEach(i => { i.bad = bad.includes(i.upc); });
    banner('error', e.message + (bad.length ? ' The highlighted item was rejected; remove or replace it.' : ''));
  }
  render();
};

load();
