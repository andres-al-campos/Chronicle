// Chronicle Export — popup script.
// Two-phase flow: Fetch (populates list from IndexedDB) → Download (selected only).
// Never uses innerHTML with variables. Reads chrome.storage.local for progress.

const els = {
  version: document.getElementById('version'),
  lastDownload: document.getElementById('lastDownload'),
  fetchAll: document.getElementById('fetchAll'),
  dateRow: document.getElementById('dateRow'),
  startDate: document.getElementById('startDate'),
  endDate: document.getElementById('endDate'),
  setupPanel: document.getElementById('setupPanel'),
  fetchBtn: document.getElementById('fetchBtn'),
  cancelBtn: document.getElementById('cancelBtn'),
  elapsed: document.getElementById('elapsed'),
  status: document.getElementById('status'),
  progressBar: document.getElementById('progressBar'),
  listPanel: document.getElementById('listPanel'),
  convList: document.getElementById('convList'),
  selectAllBtn: document.getElementById('selectAllBtn'),
  deselectAllBtn: document.getElementById('deselectAllBtn'),
  selectionCount: document.getElementById('selectionCount'),
  footer: document.getElementById('footer'),
  downloadBtn: document.getElementById('downloadBtn'),
  newExportBtn: document.getElementById('newExportBtn'),
  log: document.getElementById('log')
};

let elapsedTimer = null;

// Track conversation metadata in popup memory (lightweight — uuid, title,
// date, words). Full conversation data lives in IndexedDB only.
let convMetas = [];       // { uuid, title, date, words, changed, checked }
let fetchComplete = false;

// ──────────────────────────── helpers ────────────────────────────

function readState() {
  return new Promise((resolve) =>
    chrome.storage.local.get(['currentRun', 'lastRun', 'runLog', 'fetchedMetas', 'lastDownloadMsg'], (r) => resolve(r))
  );
}

function todayIso() { return new Date().toISOString().slice(0, 10); }
function monthAgoIso() { const d = new Date(); d.setDate(d.getDate() - 30); return d.toISOString().slice(0, 10); }

function setProgress(completed, total) {
  const pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  els.progressBar.style.width = `${pct}%`;
}

function setStatusText(text) { els.status.textContent = text; }

function formatElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

function startElapsedTicker(startedAt) {
  stopElapsedTicker();
  const tick = () => { els.elapsed.textContent = `Elapsed ${formatElapsed(Date.now() - new Date(startedAt).getTime())}`; };
  tick();
  elapsedTimer = setInterval(tick, 1000);
}
function stopElapsedTicker() { if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; } }

function formatWords(n) {
  if (n == null) return '—';
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k words`;
  return `${n} words`;
}

function applyFetchAllState() {
  if (els.fetchAll.checked) {
    els.dateRow.classList.add('dates-disabled');
  } else {
    els.dateRow.classList.remove('dates-disabled');
  }
}

// ──────────────────────────── log rendering ────────────────────────────

function clearLog() { while (els.log.firstChild) els.log.removeChild(els.log.firstChild); }

function renderLog(entries) {
  clearLog();
  for (const e of entries.slice(-50)) {
    const row = document.createElement('div');
    row.className = `log-entry ${e.level || 'ok'}`;

    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = e.level === 'error' ? '✗' : e.level === 'warn' ? '⚠' : '✓';
    row.appendChild(mark);

    const body = document.createElement('span');
    body.className = 'body';
    const parts = [];
    if (e.uuid) parts.push(e.uuid.slice(0, 8));
    if (e.project) parts.push(e.project);
    if (e.msg) parts.push(e.msg);
    if (e.retry) parts.push(`retry ${e.retry}`);
    if (e.status) parts.push(`HTTP ${e.status}`);
    body.textContent = parts.join(' · ');
    row.appendChild(body);

    if (e.bodySnippet) {
      const wrap = document.createElement('details');
      const sum = document.createElement('summary');
      sum.textContent = 'response';
      const pre = document.createElement('div');
      pre.className = 'details-row';
      pre.textContent = e.bodySnippet;
      wrap.appendChild(sum);
      wrap.appendChild(pre);
      row.appendChild(wrap);
    }
    els.log.appendChild(row);
  }
  els.log.scrollTop = els.log.scrollHeight;
}

// ──────────────────────────── conversation list ────────────────────────────

function updateSelectionCount() {
  const checked = convMetas.filter((m) => m.checked).length;
  els.selectionCount.textContent = `${checked}/${convMetas.length} selected`;
  els.downloadBtn.textContent = checked > 0 ? `Download ${checked} conversation${checked !== 1 ? 's' : ''}` : 'Download selected';
  els.downloadBtn.disabled = checked === 0;
}

function renderConvRow(meta) {
  const row = document.createElement('div');
  row.className = 'conv-row';
  row.dataset.uuid = meta.uuid;

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = meta.checked;
  cb.addEventListener('change', () => {
    meta.checked = cb.checked;
    updateSelectionCount();
  });
  row.appendChild(cb);

  const date = document.createElement('span');
  date.className = 'conv-date';
  date.textContent = meta.date || '—';
  row.appendChild(date);

  const words = document.createElement('span');
  words.className = 'conv-words';
  words.textContent = formatWords(meta.words);
  row.appendChild(words);

  const title = document.createElement('span');
  title.className = 'conv-title';
  title.textContent = meta.title || '(untitled)';
  title.title = meta.title || '(untitled)';  // full title on hover
  row.appendChild(title);

  // Click row to toggle checkbox
  row.addEventListener('click', (e) => {
    if (e.target === cb) return;
    cb.checked = !cb.checked;
    meta.checked = cb.checked;
    updateSelectionCount();
  });

  return row;
}

function renderConvList() {
  // Clear and re-render from convMetas
  while (els.convList.firstChild) els.convList.removeChild(els.convList.firstChild);
  for (const meta of convMetas) {
    els.convList.appendChild(renderConvRow(meta));
  }
  updateSelectionCount();
}

function addOrUpdateConvRow(meta) {
  // Check if row exists already (update case)
  const existing = els.convList.querySelector(`[data-uuid="${meta.uuid}"]`);
  if (existing) {
    existing.replaceWith(renderConvRow(meta));
  } else {
    els.convList.appendChild(renderConvRow(meta));
  }
  updateSelectionCount();
}

// ──────────────────────────── state-driven rendering ────────────────────────────

function renderFromState(state) {
  const { currentRun, lastRun, runLog = [], fetchedMetas, lastDownloadMsg } = state;
  renderLog(runLog);

  // Show persisted last download message on setup view
  if (lastDownloadMsg && !currentRun) {
    els.lastDownload.textContent = lastDownloadMsg;
    els.lastDownload.classList.remove('hidden');
  }

  if (currentRun) {
    const phase = currentRun.phase || 'fetch';

    if (phase === 'fetch') {
      els.fetchBtn.disabled = true;
      els.cancelBtn.classList.remove('hidden');
      els.listPanel.classList.add('active');
      if (currentRun.startedAt) startElapsedTicker(currentRun.startedAt);
      const { completed = 0, total = 0 } = currentRun;
      setStatusText(total > 0 ? `Fetching ${completed}/${total}…` : 'Listing conversations…');
      setProgress(completed, total);

      // Restore list from fetchedMetas if popup was reopened mid-fetch
      if (fetchedMetas && fetchedMetas.length > 0 && convMetas.length === 0) {
        convMetas = fetchedMetas.map((m) => ({ ...m, checked: m.checked !== false }));
        renderConvList();
      }
    }
    return;
  }

  stopElapsedTicker();
  els.cancelBtn.classList.add('hidden');
  els.fetchBtn.disabled = false;
  setProgress(0, 0);

  // If we have fetched metas and fetch is done, show the list
  if (fetchedMetas && fetchedMetas.length > 0) {
    if (convMetas.length === 0) {
      convMetas = fetchedMetas.map((m) => ({ ...m, checked: m.checked !== false }));
      renderConvList();
    }
    fetchComplete = true;
    els.listPanel.classList.add('active');
    els.footer.classList.add('active');
    els.setupPanel.style.display = 'none';
  }

  if (lastRun) {
    if (lastRun.aborted) {
      setStatusText(`Aborted: ${lastRun.error || 'unknown error'}`);
    } else if (lastRun.downloaded) {
      setStatusText(`Downloaded → ${lastRun.outputFile}`);
    } else if (lastRun.noChanges) {
      setStatusText('Nothing new since last export.');
    } else if (lastRun.fetchDone) {
      const parts = [`Fetched ${lastRun.succeeded} ok`];
      if (lastRun.failed > 0) parts.push(`${lastRun.failed} failed`);
      setStatusText(parts.join(' · ') + ' — select and download below.');
    } else {
      const parts = [`Last run: ${lastRun.succeeded || 0} ok`];
      if (lastRun.failed > 0) parts.push(`${lastRun.failed} failed`);
      if (lastRun.outputFile) parts.push(`→ ${lastRun.outputFile}`);
      setStatusText(parts.join(' · '));
    }
    if (lastRun.durationMs) {
      els.elapsed.textContent = `Took ${formatElapsed(lastRun.durationMs)}`;
    } else {
      els.elapsed.textContent = '';
    }
  } else {
    setStatusText('Ready.');
    els.elapsed.textContent = '';
  }
}

// ──────────────────────────── actions ────────────────────────────

function ensureContentScript() {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ action: 'ensureContentScript' }, (res) => {
      if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
      if (!res || !res.success) {
        // The background's messages already say how to fix the problem.
        const err = new Error(res && res.error ? res.error : 'Could not reach claude.ai tab.');
        err.hasFix = !!(res && res.error);
        return reject(err);
      }
      resolve(res.tabId);
    });
  });
}

function sendToContent(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (res) => {
      if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
      resolve(res);
    });
  });
}

async function startFetch() {
  const fetchAll = els.fetchAll.checked;
  const startDate = fetchAll ? null : els.startDate.value;
  const endDate = fetchAll ? null : els.endDate.value;

  if (!fetchAll) {
    if (!startDate || !endDate) {
      setStatusText('Pick both a start and an end date, or check "Export all".');
      return;
    }
    if (startDate > endDate) {
      setStatusText('Start date must be on or before end date.');
      return;
    }
  }

  // Reset state
  convMetas = [];
  fetchComplete = false;
  while (els.convList.firstChild) els.convList.removeChild(els.convList.firstChild);
  els.listPanel.classList.add('active');
  els.footer.classList.remove('active');
  els.fetchBtn.disabled = true;
  els.lastDownload.classList.add('hidden');
  chrome.storage.local.remove(['lastDownloadMsg']);
  setStatusText('Connecting to claude.ai…');

  try {
    const tabId = await ensureContentScript();
    await sendToContent(tabId, {
      action: 'fetchConversations',
      fetchAll,
      startDate,
      endDate
    });
    setStatusText('Fetch started…');
  } catch (err) {
    els.fetchBtn.disabled = false;
    setStatusText(err.hasFix ? err.message : `Could not start: ${err.message}. Open claude.ai and try again.`);
  }
}

async function startDownload() {
  const selectedUuids = convMetas.filter((m) => m.checked).map((m) => m.uuid);
  if (selectedUuids.length === 0) {
    setStatusText('No conversations selected.');
    return;
  }

  els.downloadBtn.disabled = true;
  setStatusText(`Building download for ${selectedUuids.length} conversations…`);

  try {
    const tabId = await ensureContentScript();
    const fetchAll = els.fetchAll.checked;
    const startDate = fetchAll ? null : els.startDate.value;
    const endDate = fetchAll ? null : els.endDate.value;
    await sendToContent(tabId, {
      action: 'downloadSelected',
      uuids: selectedUuids,
      fetchAll,
      startDate,
      endDate
    });
  } catch (err) {
    els.downloadBtn.disabled = false;
    setStatusText(`Download failed: ${err.message}`);
  }
}

async function cancelRun() {
  await new Promise((resolve) => chrome.storage.local.set({ cancelRequested: true }, resolve));
  setStatusText('Cancelling…');
}

async function resetToSetup(opts) {
  const { lastDownloadMsg } = opts || {};
  convMetas = [];
  fetchComplete = false;
  els.listPanel.classList.remove('active');
  els.footer.classList.remove('active');
  els.setupPanel.style.display = '';
  els.fetchBtn.disabled = false;
  while (els.convList.firstChild) els.convList.removeChild(els.convList.firstChild);
  clearLog();
  // Clear stored state so a fresh popup open starts clean
  await new Promise((resolve) =>
    chrome.storage.local.remove(['fetchedMetas', 'lastRun', 'runLog', 'currentRun'], resolve)
  );
  // Show or persist last download info
  if (lastDownloadMsg) {
    els.lastDownload.textContent = lastDownloadMsg;
    els.lastDownload.classList.remove('hidden');
    await new Promise((resolve) =>
      chrome.storage.local.set({ lastDownloadMsg }, resolve)
    );
  }
  setStatusText('Ready.');
  setProgress(0, 0);
  els.elapsed.textContent = '';
}

// ──────────────────────────── storage change listener ────────────────────────────

function onStorageChanged(changes, area) {
  if (area !== 'local') return;

  // Live-update the list as new metas arrive during fetch
  if (changes.fetchedMetas) {
    const metas = changes.fetchedMetas.newValue || [];
    // Add any new metas we don't have yet
    for (const m of metas) {
      const existing = convMetas.find((c) => c.uuid === m.uuid);
      if (!existing) {
        const entry = { ...m, checked: true };
        convMetas.push(entry);
        addOrUpdateConvRow(entry);
      }
    }
    // Auto-scroll to bottom of list to show new entries
    els.convList.scrollTop = els.convList.scrollHeight;
  }

  // Update status/progress from currentRun changes
  readState().then((state) => {
    const { currentRun, lastRun, runLog = [] } = state;
    renderLog(runLog);

    if (currentRun) {
      const phase = currentRun.phase || 'fetch';
      els.cancelBtn.classList.remove('hidden');
      if (currentRun.startedAt) startElapsedTicker(currentRun.startedAt);
      const { completed = 0, total = 0 } = currentRun;

      if (phase === 'fetch') {
        els.fetchBtn.disabled = true;
        setStatusText(total > 0 ? `Fetching ${completed}/${total}…` : 'Listing conversations…');
        setProgress(completed, total);
      } else if (phase === 'download') {
        setStatusText('Building download…');
      }
    } else {
      stopElapsedTicker();
      els.cancelBtn.classList.add('hidden');
      els.fetchBtn.disabled = false;

      if (lastRun) {
        if (lastRun.downloaded) {
          // Auto-reset to setup, showing what was downloaded
          const n = lastRun.succeeded || lastRun.total || 0;
          const msg = `✓ Downloaded ${n} conversation${n !== 1 ? 's' : ''} → ${lastRun.outputFile}`;
          resetToSetup({ lastDownloadMsg: msg });
          return;
        } else if (lastRun.fetchDone) {
          if (lastRun.cancelled) {
            // Cancelled fetch — go back to setup
            resetToSetup();
            return;
          }
          fetchComplete = true;
          els.footer.classList.add('active');
          const parts = [`Fetched ${lastRun.succeeded} ok`];
          if (lastRun.failed > 0) parts.push(`${lastRun.failed} failed`);
          if (lastRun.skippedUnchanged > 0) parts.push(`${lastRun.skippedUnchanged} unchanged`);
          setStatusText(parts.join(' · '));
          setProgress(0, 0);
          if (lastRun.durationMs) els.elapsed.textContent = `Took ${formatElapsed(lastRun.durationMs)}`;
        } else if (lastRun.aborted) {
          resetToSetup();
          return;
        }
      }
    }
  });
}

// ──────────────────────────── prefs ────────────────────────────

function savePrefs() {
  chrome.storage.local.set({
    prefs: {
      startDate: els.startDate.value,
      endDate: els.endDate.value,
      fetchAll: els.fetchAll.checked
    }
  });
}

function loadPrefs() {
  return new Promise((resolve) => chrome.storage.local.get(['prefs'], (r) => resolve(r.prefs || {})));
}

// ──────────────────────────── init ────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  els.version.textContent = `v${chrome.runtime.getManifest().version}`;

  const prefs = await loadPrefs();
  els.startDate.value = prefs.startDate || monthAgoIso();
  els.endDate.value = prefs.endDate || todayIso();
  els.fetchAll.checked = prefs.fetchAll !== false;  // default true
  applyFetchAllState();

  const state = await readState();
  renderFromState(state);

  chrome.storage.onChanged.addListener(onStorageChanged);

  // Input listeners
  els.startDate.addEventListener('change', savePrefs);
  els.endDate.addEventListener('change', savePrefs);
  els.fetchAll.addEventListener('change', () => { applyFetchAllState(); savePrefs(); });

  for (const input of [els.startDate, els.endDate]) {
    input.addEventListener('click', () => {
      if (typeof input.showPicker === 'function') {
        try { input.showPicker(); } catch (_) {}
      }
    });
  }

  // Buttons
  els.fetchBtn.addEventListener('click', () => startFetch());
  els.cancelBtn.addEventListener('click', () => cancelRun());
  els.downloadBtn.addEventListener('click', () => startDownload());

  els.selectAllBtn.addEventListener('click', () => {
    for (const m of convMetas) m.checked = true;
    els.convList.querySelectorAll('input[type="checkbox"]').forEach((cb) => { cb.checked = true; });
    updateSelectionCount();
  });

  els.deselectAllBtn.addEventListener('click', () => {
    for (const m of convMetas) m.checked = false;
    els.convList.querySelectorAll('input[type="checkbox"]').forEach((cb) => { cb.checked = false; });
    updateSelectionCount();
  });

  els.newExportBtn.addEventListener('click', () => resetToSetup());
});
