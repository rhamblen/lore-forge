/* Lore Forge frontend.
 *
 * Plain ES modules-free JS against the FastAPI backend, same as Persona Forge: no
 * build step, so a deploy is a container pull and a hard refresh.
 *
 * The version in the sidebar comes from /api/health on every poll — with two apps
 * on adjacent ports and separate release lines, "which version am I looking at" has
 * to be answerable at a glance.
 */

const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;

const state = {
  books: [],
  bookId: null,
  book: null,
  chapters: [],
  defaults: { chunk_chars: 1600, chunk_overlap: 200, embed_batch: 16 },
  models: [],
  logLevel: 'info',
  logCat: 'all',
  logSearch: '',
  logAutoscroll: true,
  logPersisted: false,
  view: 'intake',
  // The build this page loaded with. Compared against the server on every poll.
  build: null,
};

// --------------------------------------------------------------------------- //
// tiny helpers
// --------------------------------------------------------------------------- //

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error((data && (data.detail || data.message)) || `HTTP ${res.status}`);
  return data;
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtBytes = (n) => {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), u.length - 1);
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
};

const fmtNum = (n) => Number(n || 0).toLocaleString();

function hint(el, msg, cls = '') {
  const node = typeof el === 'string' ? $(el) : el;
  node.textContent = msg || '';
  node.className = `hint ${cls}`;
}

function setDot(id, cls) { $(id).className = `dot dot-${cls}`; }

const STATUS_PILL = { done: 'pill-ok', error: 'pill-bad', pending: 'pill-run', none: '' };

function pill(el, status, text) {
  el.className = `pill ${STATUS_PILL[status] || ''}`;
  el.textContent = text || status;
}

// --------------------------------------------------------------------------- //
// status + version
// --------------------------------------------------------------------------- //

async function refreshStatus() {
  let st;
  try {
    st = await api('/api/status');
  } catch (err) {
    setDot('ollama-dot', 'bad');
    $('ollama-value').textContent = 'backend down';
    $('ollama-meta').textContent = String(err.message || err);
    return;
  }

  // Version answers "which release"; build answers "is this the newest code" — which
  // the version cannot during a run of local iteration, when it deliberately stays put.
  $('app-version').textContent = `v${st.version} · ${st.build || '?'}`;

  // If the server's build changed since this page loaded, the page is running old
  // JavaScript. Say so, rather than leaving you to wonder whether an update landed.
  if (state.build === null) {
    state.build = st.build;
  } else if (st.build && st.build !== state.build) {
    const banner = $('stale-banner');
    if (banner) banner.hidden = false;
  }
  state.defaults = st.defaults;

  const o = st.ollama;
  setDot('ollama-dot', o.reachable ? 'ok' : 'bad');
  $('ollama-value').textContent = o.reachable ? `${o.models.length} models` : 'unreachable';
  $('ollama-meta').textContent = o.reachable ? o.url : (o.error || o.url);

  // When Ollama is down we don't KNOW whether the model is pulled — saying "not
  // pulled" would send you off pulling a model that is already there.
  setDot('embed-dot', !o.reachable ? 'unknown' : (o.embed_model_present ? 'ok' : 'bad'));
  $('embed-value').textContent = !o.reachable ? 'unknown'
    : (o.embed_model_present ? 'ready' : 'not pulled');
  $('embed-meta').textContent = !o.reachable ? `${o.embed_model} — can't check, Ollama is down`
    : o.embed_model + (o.embed_model_present ? '' : ' — pull it before indexing');

  const s = st.storage;
  setDot('storage-dot', s.mounted && s.writable ? 'ok' : 'bad');
  $('storage-value').textContent = !s.mounted ? 'not mounted' : (s.writable ? 'writable' : 'read-only');
  $('storage-meta').textContent = s.error || s.root;

  state.models = o.models || [];
  renderModelSelect();

  $('ollama-detail').innerHTML = `
    <dt>URL</dt><dd class="mono">${esc(o.url)}</dd>
    <dt>Reachable</dt><dd>${o.reachable ? 'yes' : `no — ${esc(o.error)}`}</dd>
    <dt>Embedding</dt><dd>${esc(o.embed_model)} ${o.embed_model_present ? '✓' : '✗ not pulled'}</dd>
    <dt>Generation</dt><dd>${esc(o.generate_model)} ${o.generate_model_present ? '✓' : '✗ not pulled'}
      <span class="muted">— unused until L2</span></dd>
    <dt>Models</dt><dd>${o.models.length ? esc(o.models.map((m) => m.name).join(', ')) : '—'}</dd>`;

  $('storage-detail').innerHTML = `
    <dt>Root</dt><dd class="mono">${esc(s.root)}</dd>
    <dt>Mounted</dt><dd>${s.mounted ? 'yes' : 'no'}</dd>
    <dt>Writable</dt><dd>${s.writable ? 'yes' : `no — ${esc(s.error)}`}</dd>
    <dt>Books</dt><dd>${st.books}</dd>`;
}

function renderModelSelect() {
  const sel = $('index-model');
  if (!sel) return;
  const current = sel.value || state.book?.embed_model || '';
  // Embedding models only — offering a 12B chat model here would produce a
  // confidently useless index.
  const embedders = state.models
    .map((m) => m.name)
    .filter((n) => /embed|bge|gte|e5|minilm/i.test(n));
  const options = embedders.length ? embedders : state.models.map((m) => m.name);
  sel.innerHTML = options.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  if (current && options.includes(current)) sel.value = current;
}

// --------------------------------------------------------------------------- //
// books
// --------------------------------------------------------------------------- //

async function refreshBooks() {
  state.books = await api('/api/books');
  const sel = $('book-select');
  sel.innerHTML = state.books.length
    ? state.books.map((b) => `<option value="${b.id}">${esc(b.title)}</option>`).join('')
    : '<option value="">no books yet</option>';
  if (state.bookId && state.books.some((b) => b.id === state.bookId)) {
    sel.value = String(state.bookId);
  } else if (state.books.length) {
    state.bookId = state.books[0].id;
    sel.value = String(state.bookId);
  } else {
    state.bookId = null;
  }
  renderBooksTable();
  await refreshBook();
}

function renderBooksTable() {
  const el = $('books-table');
  if (!state.books.length) {
    el.innerHTML = '<p class="muted">No books yet — add one on the Intake tab.</p>';
    return;
  }
  el.innerHTML = `<table class="book-table">
    <thead><tr><th>Title</th><th>Source</th><th>Parse</th><th>Index</th><th>Chunks</th><th>Model</th></tr></thead>
    <tbody>${state.books.map((b) => `
      <tr class="row-link ${b.id === state.bookId ? 'sel' : ''}" data-id="${b.id}">
        <td>${esc(b.title)}${b.author ? `<span class="muted"> — ${esc(b.author)}</span>` : ''}</td>
        <td class="mono">${esc(b.source_kind)} · ${fmtBytes(b.source_bytes)}</td>
        <td><span class="pill ${STATUS_PILL[b.parse_status] || ''}">${esc(b.parse_status)}</span>
            ${b.chapter_count ? `<span class="muted"> ${b.chapter_count} ch</span>` : ''}</td>
        <td><span class="pill ${STATUS_PILL[b.index_status] || ''}">${esc(b.index_status)}</span></td>
        <td>${b.chunk_count ? fmtNum(b.chunk_count) : '—'}</td>
        <td class="mono">${esc(b.embed_model || '—')}</td>
      </tr>`).join('')}</tbody></table>`;
  el.querySelectorAll('tr.row-link').forEach((row) => {
    row.onclick = () => {
      state.bookId = Number(row.dataset.id);
      $('book-select').value = String(state.bookId);
      refreshBook();
      renderBooksTable();
    };
  });
}

async function refreshBook() {
  const hasBook = Boolean(state.bookId);
  $('intake-empty').hidden = hasBook;
  $('intake-book').hidden = !hasBook;
  $('index-empty').hidden = hasBook;
  $('index-book').hidden = !hasBook;
  $('extract-empty').hidden = hasBook;
  $('extract-book').hidden = !hasBook;
  $('lorebook-empty').hidden = hasBook;
  $('lorebook-book').hidden = !hasBook;
  if (!hasBook) { state.book = null; return; }

  state.book = await api(`/api/books/${state.bookId}`);
  const b = state.book;
  state.chapters = b.chapters || [];

  pill($('parse-pill'), b.parse_status, b.parse_status);
  $('book-detail').innerHTML = `
    <dt>Source</dt><dd class="mono">${esc(b.source_file)} — ${esc(b.source_kind)}, ${fmtBytes(b.source_bytes)}</dd>
    <dt>Folder</dt><dd class="mono">${esc(b.folder)}</dd>
    <dt>Chapters</dt><dd>${b.chapter_count || '—'}</dd>
    <dt>Words</dt><dd>${b.word_count ? fmtNum(b.word_count) : '—'}</dd>
    <dt>On disk</dt><dd>${fmtBytes(b.disk_bytes)}</dd>
    <dt>SHA-256</dt><dd class="mono" style="font-size:11px">${esc(b.source_sha)}</dd>`;
  if (b.parse_message) {
    hint('parse-hint', b.parse_message, b.parse_status === 'error' ? 'bad' : 'warn');
  } else if (b.parse_status === 'done') {
    hint('parse-hint', `${b.chapter_count} chapters, ${fmtNum(b.word_count)} words.`, 'ok');
  }
  $('parse-btn').textContent = b.parse_status === 'done' ? 'Re-parse' : 'Parse to chaptered text';

  $('chapters-panel').hidden = !state.chapters.length;
  $('chapters-count').textContent = `${state.chapters.length} chapters`;
  $('chapter-list').innerHTML = state.chapters.map((c) => `
    <div class="chapter-row" data-pos="${c.position}">
      <span class="num">${c.position}</span>
      <span class="ttl">${esc(c.title)}</span>
      <span class="wc">${fmtNum(c.word_count)} w</span>
    </div>`).join('');
  $('chapter-list').querySelectorAll('.chapter-row').forEach((row) => {
    row.onclick = () => showChapter(Number(row.dataset.pos));
  });

  // Name the book the L2/L3 panels are showing, and say plainly that other books keep
  // their own data — the empty state after switching books is not data loss.
  const others = state.books.length - 1;
  const scope = `Showing <strong>${esc(b.title)}</strong>`
    + ` <span class="muted">— ${b.chapter_count || 0} chapters`
    + (others > 0 ? `; ${others} other book${others > 1 ? 's' : ''} in the library keep`
                    + ` their own rules, characters and quests` : '')
    + '</span>';
  ['extract-scope', 'lorebook-scope'].forEach((id) => {
    const el = $(id);
    if (el) el.innerHTML = scope;
  });

  pill($('index-pill'), b.index_status, b.index_status);
  $('index-chunk').value = b.chunk_chars || state.defaults.chunk_chars;
  $('index-overlap').value = b.chunk_overlap || state.defaults.chunk_overlap;
  if (!$('index-batch').value) $('index-batch').value = state.defaults.embed_batch;
  if (b.embed_model) {
    const sel = $('index-model');
    if ([...sel.options].some((o) => o.value === b.embed_model)) sel.value = b.embed_model;
  }
  $('index-btn').textContent = b.index_status === 'done' ? 'Rebuild index' : 'Build index';
  $('index-btn').disabled = b.parse_status !== 'done';
  if (b.parse_status !== 'done') {
    hint('index-hint', 'Parse the book first.', 'warn');
  } else if (b.index_message) {
    hint('index-hint', b.index_message, b.index_status === 'error' ? 'bad' : 'warn');
  } else if (b.index_status === 'done') {
    hint('index-hint',
      `${fmtNum(b.chunk_count)} chunks · ${b.embed_dims} dims · ${b.embed_model}`, 'ok');
  }
}

async function showChapter(position) {
  const el = $('chapter-text');
  el.hidden = false;
  el.textContent = 'loading…';
  try {
    const ch = await api(`/api/books/${state.bookId}/chapters/${position}`);
    el.textContent = `${ch.title}\n${'─'.repeat(Math.min(ch.title.length, 60))}\n\n${ch.text}`;
  } catch (err) {
    el.textContent = `could not load chapter: ${err.message}`;
  }
}

// --------------------------------------------------------------------------- //
// jobs — drive the progress bars
// --------------------------------------------------------------------------- //

async function refreshJobs() {
  let jobs = [];
  try { jobs = await api('/api/jobs?limit=12'); } catch { return; }

  $('jobs-list').innerHTML = jobs.length ? jobs.map((j) => `
    <div class="job-row">
      <span class="kind">${esc(j.kind)}</span>
      <span class="pill ${STATUS_PILL[j.status] || (j.status === 'running' ? 'pill-run' : '')}">${esc(j.status)}</span>
      <span class="msg">${esc(j.message || j.stage || '')}</span>
      <span class="muted" style="font-size:11px">#${j.id}</span>
    </div>`).join('') : '<p class="muted">none yet</p>';

  const mine = jobs.filter((j) => j.book_id === state.bookId);
  applyJob(mine.find((j) => j.kind === 'parse'), 'parse');
  applyJob(mine.find((j) => j.kind === 'index'), 'index');

  // Both extraction kinds share one progress bar — the engine runs jobs serially, so
  // only one can ever be live.
  const ex = mine.find((j) => j.kind === 'extract_rules' || j.kind === 'extract_world');
  applyJob(ex, 'extract');
  if (ex && (ex.status === 'running' || ex.status === 'queued')) {
    state.extractWasLive = true;
  } else if (state.extractWasLive) {
    // Finished since the last poll: refresh the tables that just changed.
    state.extractWasLive = false;
    refreshCharacters();
    refreshRules();
    refreshQuests();
    refreshEntries();
  }
}

// Which buttons a running job of each kind should disable. `extract` has two (rules and
// world), which is why this is a list rather than a single `${kind}-btn` lookup.
const JOB_BUTTONS = {
  parse: ['parse-btn'],
  index: ['index-btn'],
  extract: ['extract-rules-btn', 'extract-world-btn', 'extract-quests-btn', 'census-btn'],
};

function applyJob(job, kind) {
  const live = job && (job.status === 'queued' || job.status === 'running');
  const bar = $(`${kind}-bar`);
  const cancel = $(`${kind}-cancel-btn`);
  if (bar) bar.hidden = !live;
  if (cancel) cancel.hidden = !live;
  (JOB_BUTTONS[kind] || []).forEach((id) => {
    const btn = $(id);
    if (btn) {
      btn.disabled = Boolean(live)
        || (kind === 'index' && state.book?.parse_status !== 'done');
    }
  });
  if (!job) return;
  if (live) {
    const pct = Math.round((job.progress || 0) * 100);
    const fill = $(`${kind}-fill`);
    if (fill) fill.style.width = `${Math.max(pct, 3)}%`;
    hint(`${kind}-hint`, job.message || job.stage || 'queued…', '');
    if (cancel) cancel.onclick = async () => {
      try { await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }); } catch (e) { /* shown next poll */ }
    };
  } else if (job.status === 'done' || job.status === 'error') {
    const fill = $(`${kind}-fill`);
    if (!fill) return;
    const wasLive = fill.style.width && fill.style.width !== '0%';
    fill.style.width = '0%';
    if (wasLive) { refreshBook(); refreshBooks(); }
  }
}

// --------------------------------------------------------------------------- //
// actions
// --------------------------------------------------------------------------- //

$('upload-btn').onclick = async () => {
  const file = $('upload-file').files[0];
  if (!file) { hint('upload-hint', 'Choose a file first.', 'bad'); return; }
  const fd = new FormData();
  fd.append('file', file);
  fd.append('title', $('upload-title').value.trim());
  fd.append('author', $('upload-author').value.trim());
  $('upload-btn').disabled = true;
  hint('upload-hint', `uploading ${file.name} (${fmtBytes(file.size)})…`);
  try {
    const book = await api('/api/books', { method: 'POST', body: fd });
    state.bookId = book.id;
    hint('upload-hint', `added “${book.title}” as ${book.source_kind}.`, 'ok');
    $('upload-file').value = '';
    $('upload-title').value = '';
    $('upload-author').value = '';
    await refreshBooks();
    if ($('upload-autoparse').checked) await startParse();
  } catch (err) {
    hint('upload-hint', String(err.message || err), 'bad');
  } finally {
    $('upload-btn').disabled = false;
  }
};

async function startParse() {
  try {
    await api(`/api/books/${state.bookId}/parse`, { method: 'POST' });
    hint('parse-hint', 'queued…');
    await refreshJobs();
  } catch (err) {
    hint('parse-hint', String(err.message || err), 'bad');
  }
}

$('parse-btn').onclick = () => state.bookId && startParse();

$('index-btn').onclick = async () => {
  try {
    await api(`/api/books/${state.bookId}/index`, {
      method: 'POST',
      body: JSON.stringify({
        model: $('index-model').value,
        chunk_chars: Number($('index-chunk').value) || 0,
        chunk_overlap: Number($('index-overlap').value) || 0,
        batch: Number($('index-batch').value) || 0,
      }),
    });
    hint('index-hint', 'queued…');
    await refreshJobs();
  } catch (err) {
    hint('index-hint', String(err.message || err), 'bad');
  }
};

$('query-btn').onclick = async () => {
  const question = $('query-input').value.trim();
  if (!question) { hint('query-hint', 'Type a question.', 'bad'); return; }
  $('query-btn').disabled = true;
  hint('query-hint', 'retrieving…');
  try {
    const res = await api(`/api/books/${state.bookId}/query`, {
      method: 'POST',
      body: JSON.stringify({ question, k: Number($('query-k').value) || 6 }),
    });
    hint('query-hint',
      `${res.hits.length} of ${fmtNum(res.searched)} chunks · ${res.model}`, 'ok');
    $('query-results').innerHTML = res.hits.map((h) => `
      <div class="hit">
        <div class="hit-head">
          <span class="hit-score">${h.score.toFixed(3)}</span>
          <span class="hit-cite">${esc(h.citation)}</span>
        </div>
        <div class="hit-text">${esc(h.text)}</div>
      </div>`).join('');
  } catch (err) {
    hint('query-hint', String(err.message || err), 'bad');
    $('query-results').innerHTML = '';
  } finally {
    $('query-btn').disabled = false;
  }
};

$('query-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('query-btn').click(); });

$('delete-book-btn').onclick = async () => {
  const b = state.book;
  if (!b) return;
  const purge = confirm(
    `Delete “${b.title}” from the library?\n\n`
    + 'OK      = also delete its folder and all extracted files\n'
    + 'Cancel  = keep the files, remove only the database entry\n\n'
    + `Folder: ${b.folder}`);
  try {
    await api(`/api/books/${b.id}?purge_files=${purge}`, { method: 'DELETE' });
    state.bookId = null;
    await refreshBooks();
  } catch (err) {
    hint('parse-hint', String(err.message || err), 'bad');
  }
};

$('report-btn').onclick = () => openReport('parse');
$('index-report-btn').onclick = () => openReport('index');

async function openReport(which) {
  const target = which === 'parse' ? 'parse-hint' : 'index-hint';
  try {
    const rep = await api(`/api/books/${state.bookId}/report?which=${which}`);
    const w = window.open('', '_blank');
    w.document.write(`<pre style="background:#0b0d13;color:#e6e9ef;padding:20px;font:12.5px/1.6 ui-monospace,Consolas,monospace">${
      esc(JSON.stringify(rep, null, 2))}</pre>`);
    w.document.title = `${which} report`;
  } catch (err) {
    hint(target, String(err.message || err), 'bad');
  }
}

$('new-book-btn').onclick = () => { switchView('intake'); $('upload-file').click(); };
$('book-select').onchange = (e) => {
  state.bookId = Number(e.target.value) || null;
  refreshBook();
  renderBooksTable();
};

// --------------------------------------------------------------------------- //
// L2 — extract & curate
// --------------------------------------------------------------------------- //

const KIND_COLOURS = {
  xp: 'pill-run', level: 'pill-run', attribute: 'pill-ok', skill: 'pill-ok',
  class: 'pill-ok', currency: 'pill-warn', cap: 'pill-bad', penalty: 'pill-bad',
};

function renderExtractModels() {
  const sel = $('extract-model');
  if (!sel) return;
  const current = sel.value;
  // Generation models only — an embedding model cannot answer an extraction prompt.
  const gen = state.models.map((m) => m.name).filter((n) => !/embed|bge|gte|e5|minilm/i.test(n));
  sel.innerHTML = gen.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  if (current && gen.includes(current)) sel.value = current;
  else if (gen.includes('gemma3:12b')) sel.value = 'gemma3:12b';
}

function citeTitle(citations) {
  return (citations || [])
    .map((c) => `ch.${c.chapter}${c.source_ref ? ` · ${c.source_ref}` : ''}`)
    .join('\n');
}

async function refreshRules() {
  if (!state.bookId) return;
  let data;
  try { data = await api(`/api/books/${state.bookId}/rules`); } catch { return; }
  const c = data.counts;
  $('rules-count').textContent =
    `${c.total} · ${c.kept} kept · ${c.discarded} discarded`;

  if (!data.rules.length) {
    $('rules-out').innerHTML = emptyMsg('No rules extracted for this book');
    $('conflicts-out').innerHTML = '';
    return;
  }

  // Same mechanic filed under two kinds — surfaced, never auto-merged, because two
  // rules can legitimately share a name.
  const byName = {};
  data.rules.forEach((r) => {
    const k = r.name.toLowerCase();
    (byName[k] = byName[k] || []).push(r);
  });
  const conflicts = Object.values(byName).filter(
    (g) => new Set(g.map((r) => r.kind)).size > 1);
  $('conflicts-out').innerHTML = conflicts.length ? `
    <p class="hint warn" style="margin-bottom:10px">
      ${conflicts.length} name(s) filed under more than one kind —
      ${esc(conflicts.map((g) => g[0].name).join(', '))}. Merge by hand if they are one rule.
    </p>` : '';

  $('rules-out').innerHTML = `<table class="book-table">
    <thead><tr><th>Kind</th><th>Name</th><th>Statement</th><th>Conf.</th><th>Cites</th><th></th></tr></thead>
    <tbody>${data.rules.map((r) => `
      <tr data-id="${r.id}" style="${r.status === 'discarded' ? 'opacity:.4' : ''}">
        <td><span class="pill ${KIND_COLOURS[r.kind] || ''}">${esc(r.kind)}</span></td>
        <td>${esc(r.name)}${r.edited ? ' <span class="pill">edited</span>' : ''}</td>
        <td>${esc(r.statement)}</td>
        <td>${r.confidence === 'stated' ? '<span class="pill pill-ok">stated</span>'
                                        : '<span class="pill">implied</span>'}</td>
        <td title="${esc(citeTitle(r.citations))}">${r.citation_count}</td>
        <td style="white-space:nowrap">
          <button class="btn btn-sm" data-act="keep" ${r.status === 'kept' ? 'disabled' : ''}>keep</button>
          <button class="btn btn-sm btn-danger" data-act="discard" ${r.status === 'discarded' ? 'disabled' : ''}>✕</button>
        </td>
      </tr>`).join('')}</tbody></table>`;

  $('rules-out').querySelectorAll('button[data-act]').forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.closest('tr').dataset.id;
      try { await api(`/api/rules/${id}/${btn.dataset.act}`, { method: 'POST' }); }
      catch (err) { hint('extract-hint', String(err.message || err), 'bad'); }
      refreshRules();
    };
  });
}

async function refreshEntries() {
  if (!state.bookId) return;
  let data;
  try { data = await api(`/api/books/${state.bookId}/entries`); } catch { return; }
  const c = data.counts;
  $('entries-count').textContent = `${c.total} · ${c.kept} kept · ${c.discarded} discarded`;

  if (!data.entries.length) {
    $('entries-out').innerHTML = emptyMsg('No world entities extracted for this book');
    return;
  }
  $('entries-out').innerHTML = `<table class="book-table">
    <thead><tr><th>Kind</th><th>Name</th><th>Keys</th><th>Summary</th><th>Cites</th><th></th></tr></thead>
    <tbody>${data.entries.map((e) => `
      <tr data-id="${e.id}" style="${e.status === 'discarded' ? 'opacity:.4' : ''}">
        <td><span class="pill">${esc(e.kind)}</span></td>
        <td>${esc(e.name)}${e.edited ? ' <span class="pill">edited</span>' : ''}</td>
        <td title="${esc([e.name, ...e.aliases].join(', '))}">
          <span class="pill ${e.key_count > 1 ? 'pill-ok' : 'pill-warn'}">${e.key_count}</span>
        </td>
        <td>${esc((e.summary || '').slice(0, 150))}</td>
        <td title="${esc(citeTitle(e.citations))}">${e.citation_count}</td>
        <td style="white-space:nowrap">
          <button class="btn btn-sm" data-act="keep" ${e.status === 'kept' ? 'disabled' : ''}>keep</button>
          <button class="btn btn-sm btn-danger" data-act="discard" ${e.status === 'discarded' ? 'disabled' : ''}>✕</button>
        </td>
      </tr>`).join('')}</tbody></table>
    <p class="hint">A key count of 1 means the entry only fires on its exact name —
      add aliases or it will rarely trigger in chat.</p>`;

  $('entries-out').querySelectorAll('button[data-act]').forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.closest('tr').dataset.id;
      try { await api(`/api/entries/${id}/${btn.dataset.act}`, { method: 'POST' }); }
      catch (err) { hint('extract-hint', String(err.message || err), 'bad'); }
      refreshEntries();
    };
  });
}

// An empty table is ambiguous when several books exist: it can mean "not run yet" or
// "you are looking at a different book". Always say which book, and never let it read
// as data loss.
function emptyMsg(what) {
  const title = state.book ? state.book.title : 'this book';
  const others = state.books.length - 1;
  return `<p class="muted">${esc(what)} — <strong>${esc(title)}</strong>.`
    + (others > 0 ? ` Other books keep their own data; switch with the Book selector.` : '')
    + `</p>`;
}

const TIER_PILL = { primary: 'pill-ok', secondary: 'pill-run', filler: '' };
const TIERS = ['primary', 'secondary', 'filler'];

async function refreshCharacters() {
  if (!state.bookId) return;
  let data;
  try { data = await api(`/api/books/${state.bookId}/characters`); } catch { return; }
  const c = data.counts;
  $('chars-count').textContent =
    `${c.total} · ${c.primary} primary · ${c.secondary} secondary · ${c.filler} filler`;

  if (!data.characters.length) {
    $('chars-out').innerHTML = emptyMsg('No census has been run for this book');
    return;
  }
  $('chars-out').innerHTML = `<table class="book-table">
    <thead><tr><th>Tier</th><th>Name</th><th>Also known as</th><th>Mentions</th>
      <th>Chapters</th><th>Speaks</th><th>Why this tier</th><th></th></tr></thead>
    <tbody>${data.characters.map((ch) => `
      <tr data-id="${ch.id}" style="${ch.status === 'discarded' ? 'opacity:.4' : ''}">
        <td>
          <select data-role="tier" style="width:auto;padding:2px 6px;font-size:12px">
            ${TIERS.map((t) => `<option value="${t}" ${t === ch.tier ? 'selected' : ''}>${t}</option>`).join('')}
          </select>
          ${ch.tier_locked ? '<span class="pill" title="set by hand; a re-census will not move it">🔒</span>' : ''}
        </td>
        <td>${esc(ch.name)}${ch.edited ? ' <span class="pill">edited</span>' : ''}
            ${ch.note ? `<div class="muted" style="font-size:11px">${esc(ch.note)}</div>` : ''}</td>
        <td class="muted" style="font-size:12px">${esc(ch.aliases.join(', ')) || '—'}</td>
        <td>${ch.mentions}</td>
        <td>${ch.chapter_count} <span class="muted" style="font-size:11px">(${ch.first_chapter}–${ch.last_chapter})</span></td>
        <td><span class="pill ${ch.dialogue_hits ? 'pill-ok' : ''}">${ch.dialogue_hits}</span></td>
        <td class="muted" style="font-size:11px">${esc(ch.tier_reason)}</td>
        <td style="white-space:nowrap">
          <button class="btn btn-sm" data-act="context" title="Show passages where this name appears">context</button>
          <select data-role="merge" style="width:auto;padding:2px 6px;font-size:12px"
                  title="Fold this character into another — the name survives as an alias">
            <option value="">merge into…</option>
            ${data.characters.filter((o) => o.id !== ch.id).map((o) =>
              `<option value="${o.id}">${esc(o.name)}</option>`).join('')}
          </select>
          <button class="btn btn-sm btn-danger" data-act="discard" ${ch.status === 'discarded' ? 'disabled' : ''}>✕</button>
        </td>
      </tr>
      <tr class="context-row" data-for="${ch.id}" hidden><td colspan="8"></td></tr>`).join('')}</tbody></table>
    <p class="hint">Tier is computed from the evidence, not asked of the model. Change it
      and it locks — a re-census will not move it back. Tier decides how much detail pass 2
      writes, and how many expression sprites Persona Forge renders.</p>`;

  $('chars-out').querySelectorAll('select[data-role="tier"]').forEach((sel) => {
    sel.onchange = async () => {
      const id = sel.closest('tr').dataset.id;
      try { await api(`/api/characters/${id}/tier/${sel.value}`, { method: 'POST' }); }
      catch (err) { hint('extract-hint', String(err.message || err), 'bad'); }
      refreshCharacters();
    };
  });
  $('chars-out').querySelectorAll('button[data-act="discard"]').forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.closest('tr').dataset.id;
      try { await api(`/api/characters/${id}/discard`, { method: 'POST' }); }
      catch (err) { hint('extract-hint', String(err.message || err), 'bad'); }
      refreshCharacters();
    };
  });

  // Context viewer — the thing that makes a merge judgeable. "Mom" shares no words with
  // "Diane Fitzgerald", so no rule can propose that pairing; reading two lines settles it.
  $('chars-out').querySelectorAll('button[data-act="context"]').forEach((btn) => {
    btn.onclick = async () => {
      const row = btn.closest('tr');
      const id = row.dataset.id;
      const target = $('chars-out').querySelector(`tr.context-row[data-for="${id}"]`);
      if (!target.hidden) { target.hidden = true; return; }
      const cell = target.querySelector('td');
      cell.innerHTML = '<p class="muted">loading…</p>';
      target.hidden = false;
      try {
        const d = await api(`/api/books/${state.bookId}/characters/${id}/mentions?limit=10`);
        cell.innerHTML = d.mentions.length ? `
          <div class="muted" style="font-size:12px;margin-bottom:6px">
            matching: ${esc([d.character.name, ...d.character.aliases].join(', '))}
          </div>
          ${d.mentions.map((m) => `
            <div class="hit">
              <div class="hit-head">
                <span class="hit-cite">ch.${m.chapter} · ${esc(m.chapter_title)}</span>
                <span class="pill">${esc(m.matched)}</span>
              </div>
              <div class="hit-text">…${esc(m.text)}…</div>
            </div>`).join('')}`
          : '<p class="muted">no mentions found</p>';
      } catch (err) {
        cell.innerHTML = `<p class="hint bad">${esc(String(err.message || err))}</p>`;
      }
    };
  });

  $('chars-out').querySelectorAll('select[data-role="merge"]').forEach((sel) => {
    sel.onchange = async () => {
      const absorbId = sel.closest('tr').dataset.id;
      const keepId = sel.value;
      if (!keepId) return;
      const keepName = sel.options[sel.selectedIndex].text;
      const absorbName = sel.closest('tr').querySelector('td:nth-child(2)').textContent.trim();
      sel.value = '';
      if (!confirm(`Merge "${absorbName}" into "${keepName}"?\n\n`
        + `"${absorbName}" becomes an alias, so it still triggers the lorebook, and a `
        + `future census will not split them apart again.`)) return;
      try {
        const row = await api(
          `/api/books/${state.bookId}/characters/${keepId}/merge/${absorbId}`,
          { method: 'POST' });
        hint('extract-hint', `merged into ${row.name} (${row.mentions} mentions, `
          + `${row.chapter_count} chapters, tier ${row.tier})`, 'ok');
      } catch (err) {
        hint('extract-hint', String(err.message || err), 'bad');
      }
      refreshCharacters();
    };
  });
}

const OUTCOME_PILL = {
  completed: 'pill-ok', failed: 'pill-bad', declined: 'pill-bad',
  ongoing: 'pill-run', accepted: 'pill-run',
};

async function refreshQuests() {
  if (!state.bookId) return;
  let data;
  try { data = await api(`/api/books/${state.bookId}/quests`); } catch { return; }
  const c = data.counts;
  $('quests-count').textContent = `${c.total} · ${c.kept} kept · ${c.discarded} discarded`;

  if (!data.quests.length) {
    $('quests-out').innerHTML = emptyMsg('No quests extracted for this book');
    return;
  }
  $('quests-out').innerHTML = `<table class="book-table">
    <thead><tr><th>Ch.</th><th>Quest</th><th>Kind</th><th>Objective</th>
      <th>Reward</th><th>Penalty</th><th>Outcome</th><th></th></tr></thead>
    <tbody>${data.quests.map((q) => `
      <tr data-id="${q.id}" style="${q.status === 'discarded' ? 'opacity:.4' : ''}">
        <td class="mono">${q.first_chapter || '—'}</td>
        <td>${esc(q.name)}${q.edited ? ' <span class="pill">edited</span>' : ''}</td>
        <td><span class="pill">${esc(q.kind)}</span></td>
        <td>${esc((q.objective || '').slice(0, 90))}</td>
        <td>${esc((q.reward || '—').slice(0, 60))}</td>
        <td>${esc((q.penalty || '—').slice(0, 60))}</td>
        <td><span class="pill ${OUTCOME_PILL[q.outcome] || ''}">${esc(q.outcome)}</span></td>
        <td style="white-space:nowrap">
          <button class="btn btn-sm" data-act="keep" ${q.status === 'kept' ? 'disabled' : ''}>keep</button>
          <button class="btn btn-sm btn-danger" data-act="discard" ${q.status === 'discarded' ? 'disabled' : ''}>✕</button>
        </td>
      </tr>`).join('')}</tbody></table>
    <p class="hint">Each quest carries its own reward and penalty — those are that
      quest's terms, not rules about all quests.</p>`;

  $('quests-out').querySelectorAll('button[data-act]').forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.closest('tr').dataset.id;
      try { await api(`/api/quests/${id}/${btn.dataset.act}`, { method: 'POST' }); }
      catch (err) { hint('extract-hint', String(err.message || err), 'bad'); }
      refreshQuests();
    };
  });
}

$('preview-btn').onclick = async () => {
  hint('extract-hint', 'scoring passages…');
  try {
    const d = await api(`/api/books/${state.bookId}/extract/preview?limit=8`);
    const s = d.stats;
    hint('extract-hint',
      `${s.chunks_selected}/${s.chunks_total} passages selected (${Math.round(s.selection_rate * 100)}%) — `
      + `${s.chunks_total - s.chunks_selected} model calls avoided`, 'ok');
    $('preview-out').innerHTML = `<div style="margin-top:12px">${d.top.map((t) => `
      <div class="hit">
        <div class="hit-head">
          <span class="hit-score">${t.score}</span>
          <span class="hit-cite">ch.${t.chapter} · ${esc(t.chapter_title)}</span>
        </div>
        <div class="muted" style="font-size:12px">${esc(t.reasons.join('; '))}</div>
      </div>`).join('')}</div>`;
  } catch (err) {
    hint('extract-hint', String(err.message || err), 'bad');
    $('preview-out').innerHTML = '';
  }
};

async function startExtract(kind) {
  const body = JSON.stringify({
    model: $('extract-model').value,
    limit: Number($('extract-limit').value) || 0,
  });
  try {
    await api(`/api/books/${state.bookId}/extract/${kind}`, { method: 'POST', body });
    hint('extract-hint', 'queued…');
    await refreshJobs();
  } catch (err) {
    hint('extract-hint', String(err.message || err), 'bad');
  }
}

$('extract-rules-btn').onclick = () => startExtract('rules');
$('extract-world-btn').onclick = () => startExtract('world');
$('extract-quests-btn').onclick = () => startExtract('quests');

$('census-btn').onclick = async () => {
  try {
    await api(`/api/books/${state.bookId}/census`, {
      method: 'POST',
      body: JSON.stringify({ model: $('extract-model').value, limit: 0 }),
    });
    hint('extract-hint', 'census queued…');
    await refreshJobs();
  } catch (err) {
    hint('extract-hint', String(err.message || err), 'bad');
  }
};

$('clear-chars-btn').onclick = async () => {
  if (!confirm('Remove proposed characters? Edited rows and locked tiers are kept.')) return;
  await api(`/api/books/${state.bookId}/characters`, { method: 'DELETE' });
  refreshCharacters();
};

$('clear-quests-btn').onclick = async () => {
  if (!confirm('Remove proposed quests? Curated and edited quests are kept.')) return;
  await api(`/api/books/${state.bookId}/quests`, { method: 'DELETE' });
  refreshQuests();
};

$('clear-rules-btn').onclick = async () => {
  if (!confirm('Remove proposed rules? Curated and edited rules are kept.')) return;
  await api(`/api/books/${state.bookId}/rules`, { method: 'DELETE' });
  refreshRules();
};
$('clear-entries-btn').onclick = async () => {
  if (!confirm('Remove proposed entries? Curated and edited entries are kept.')) return;
  await api(`/api/books/${state.bookId}/entries`, { method: 'DELETE' });
  refreshEntries();
};

// --------------------------------------------------------------------------- //
// L3 — lorebook
// --------------------------------------------------------------------------- //

function renderLorebook(d) {
  $('lb-stats-panel').hidden = false;
  $('lb-install-panel').hidden = false;
  $('lb-entries-panel').hidden = false;
  $('lb-download-btn').hidden = false;
  pill($('lb-pill'), 'done', `${d.stats.entries} entries`);

  const s = d.stats;
  $('lb-stats').innerHTML = `
    <dt>File</dt><dd class="mono">${esc(d.filename || '')}</dd>
    <dt>Entries</dt><dd>${s.entries}</dd>
    <dt>By kind</dt><dd>${esc(Object.entries(s.by_kind).map(([k, v]) => `${k} ${v}`).join(' · ')) || '—'}</dd>
    <dt>Keys</dt><dd>${s.keys_total} total · ${s.keys_mean} per entry</dd>
    ${d.path ? `<dt>Path</dt><dd class="mono" style="font-size:11px">${esc(d.path)}</dd>` : ''}`;

  // The failure mode that matters: an entry with one key fires only on its exact name.
  $('lb-keywarn').textContent = s.entries_with_one_key
    ? `${s.entries_with_one_key} entry(s) have only one key — those will rarely fire in chat. `
      + 'Add aliases on the L2 tab and recompile.'
    : 'Every entry has more than one key.';
  $('lb-keywarn').className = 'hint ' + (s.entries_with_one_key ? 'warn' : 'ok');

  $('lb-install').textContent =
    `Copy from:\n  <lore-builds>/${state.book?.slug || ''}/st-import/worlds/${d.filename}\n\n`
    + `To:\n  \\\\192.168.1.33\\appdata\\STConfig\\Data\\default-user\\worlds\\${d.filename}\n\n`
    + `Then in SillyTavern: World Info -> select "${(d.filename || '').replace(/\.json$/, '')}".\n`
    + `Or use the Download button and import via World Info -> Import.`;

  if (d.world) {
    const entries = Object.values(d.world.entries || {});
    $('lb-entries').innerHTML = `<table class="book-table">
      <thead><tr><th>Order</th><th>Comment</th><th>Keys</th><th>Content</th></tr></thead>
      <tbody>${entries.map((e) => `
        <tr>
          <td>${e.order}</td>
          <td>${esc(e.comment)}</td>
          <td><span class="pill ${e.key.length > 1 ? 'pill-ok' : 'pill-warn'}">${e.key.length}</span>
              <span class="muted" style="font-size:11px">${esc(e.key.join(', ').slice(0, 60))}</span></td>
          <td>${esc((e.content || '').slice(0, 120))}</td>
        </tr>`).join('')}</tbody></table>`;
  }
}

function renderBookPicker() {
  const el = $('lb-books');
  if (!el) return;
  const others = state.books.filter((b) => b.id !== state.bookId);
  el.innerHTML = others.length
    ? others.map((b) => `<button class="chip" data-id="${b.id}">${esc(b.title)}</button>`).join('')
    : '<span class="muted" style="font-size:12px">only one book in the library</span>';
  el.querySelectorAll('.chip').forEach((chip) => {
    chip.onclick = () => chip.classList.toggle('on');
  });
}

function selectedExtraBooks() {
  return [...document.querySelectorAll('#lb-books .chip.on')].map((c) => Number(c.dataset.id));
}

$('lb-build-btn').onclick = async () => {
  hint('lb-hint', 'compiling…');
  try {
    const also = selectedExtraBooks();
    const d = await api(`/api/books/${state.bookId}/lorebook`, {
      method: 'POST',
      body: JSON.stringify({
        include_rules: $('lb-include-rules').checked,
        include_quests: $('lb-include-quests').checked,
        kept_only: $('lb-kept-only').checked,
        also_books: also,
        name: $('lb-name').value.trim(),
      }),
    });
    hint('lb-hint',
      `compiled ${d.stats.entries} entries from ${d.sources.entities} entities, `
      + `${d.sources.quests} quests and ${d.sources.rules} rules`
      + (also.length ? ` across ${d.books.length} books.` : '.'), 'ok');
    const full = await api(`/api/books/${state.bookId}/lorebook`);
    renderLorebook(full);
  } catch (err) {
    hint('lb-hint', String(err.message || err), 'bad');
  }
};

$('lb-download-btn').onclick = () => {
  window.open(`/api/books/${state.bookId}/lorebook/download`, '_blank');
};

async function refreshLorebook() {
  if (!state.bookId) return;
  try {
    renderLorebook(await api(`/api/books/${state.bookId}/lorebook`));
  } catch {
    // 404 = not compiled yet, which is the normal starting state.
    pill($('lb-pill'), 'none', 'not compiled');
    $('lb-stats-panel').hidden = true;
    $('lb-install-panel').hidden = true;
    $('lb-entries-panel').hidden = true;
    $('lb-download-btn').hidden = true;
  }
}

// --------------------------------------------------------------------------- //
// views
// --------------------------------------------------------------------------- //

function switchView(view) {
  state.view = view;
  document.querySelectorAll('.nav-item').forEach((a) => {
    a.classList.toggle('is-active', a.dataset.view === view);
  });
  document.querySelectorAll('.view').forEach((s) => {
    s.hidden = s.id !== `view-${view}`;
  });
  if (view === 'logs') refreshLogs();
  if (view === 'extract') {
    refreshCharacters(); refreshRules(); refreshQuests(); refreshEntries();
  }
  if (view === 'lorebook') { renderBookPicker(); refreshLorebook(); }
}

document.querySelectorAll('.nav-item').forEach((a) => {
  a.onclick = (e) => { e.preventDefault(); switchView(a.dataset.view); };
});

// --------------------------------------------------------------------------- //
// logs
// --------------------------------------------------------------------------- //

const CATS = ['all', 'boot', 'integration', 'process', 'local', 'api'];
$('log-cats').innerHTML = CATS.map((c) =>
  `<button class="chip cat ${c === 'all' ? 'on' : ''}" data-cat="${c}">${c}</button>`).join('');
$('log-cats').querySelectorAll('.chip').forEach((chip) => {
  chip.onclick = () => {
    state.logCat = chip.dataset.cat;
    $('log-cats').querySelectorAll('.chip').forEach((c) => c.classList.toggle('on', c === chip));
    refreshLogs();
  };
});
$('log-level').onchange = (e) => { state.logLevel = e.target.value; refreshLogs(); };
$('log-search').oninput = (e) => { state.logSearch = e.target.value; refreshLogs(); };
$('log-autoscroll').onclick = () => {
  state.logAutoscroll = !state.logAutoscroll;
  $('log-autoscroll').classList.toggle('on', state.logAutoscroll);
};
$('log-persisted').onclick = () => {
  state.logPersisted = !state.logPersisted;
  $('log-persisted').classList.toggle('on', state.logPersisted);
  refreshLogs();
};

async function refreshLogs() {
  if (state.view !== 'logs') return;
  const qs = new URLSearchParams({
    level: state.logLevel,
    category: state.logCat,
    limit: '400',
    persisted: String(state.logPersisted),
  });
  if (state.logSearch) qs.set('search', state.logSearch);
  let data;
  try { data = await api(`/api/logs?${qs}`); } catch { return; }

  const view = $('logview');
  $('log-count').textContent = data.items.length;
  $('log-file').textContent = data.stats.file_exists
    ? `${data.stats.file} · ${fmtBytes(data.stats.file_bytes)}` : '';
  view.innerHTML = data.items.length ? data.items.map((r) => {
    const t = (r.ts || '').slice(11, 23);
    const detail = r.detail ? ` <span class="dt">${esc(JSON.stringify(r.detail))}</span>` : '';
    return `<div class="logline ${r.level}">
      <span class="t">${esc(t)}</span>
      <span class="lv">${esc(r.level.toUpperCase())}</span>
      <span class="tg">[${esc(r.category)}]</span>
      <span class="mg">${esc(r.message)}${detail}</span>
    </div>`;
  }).join('') : '<div class="empty">no records match</div>';
  if (state.logAutoscroll) view.scrollTop = view.scrollHeight;
}

// --------------------------------------------------------------------------- //
// boot
// --------------------------------------------------------------------------- //

async function tick() {
  await refreshStatus();
  await refreshJobs();
  await refreshLogs();
}

(async function init() {
  switchView('intake');
  await refreshStatus();
  await refreshBooks();
  await refreshJobs();
  setInterval(tick, POLL_MS);
})();
