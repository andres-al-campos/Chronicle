// Chronicle Export — content script.
// Runs on claude.ai. Performs all authenticated fetches using the user's
// session cookie (credentials: 'include'), stores full conversations in
// IndexedDB, and lets the popup select which ones to download.

// Double-injection guard (pattern from agoramachina/claude-exporter content.js:8-12).
if (window.__chronicleExportLoaded) {
  console.log('[Chronicle] content script already loaded, skipping');
} else {
  window.__chronicleExportLoaded = true;

  const EXTENSION_VERSION = chrome.runtime.getManifest().version;
  const FETCH_DELAY_MS = 500;
  const BACKOFF_SCHEDULE_MS = [1000, 2000, 4000];
  const MAX_RETRIES = BACKOFF_SCHEDULE_MS.length;
  const LOG_CAP = 500;

  const DB_NAME = 'ChronicleExport';
  const DB_VERSION = 1;
  const STORE_NAME = 'conversations';

  // Filename-safe character regex (from fork content.js:345).
  const INVALID_FILENAME_CHARS = /[<>:"/\\|?*\x00-\x1f]/g;

  function sanitizeFilenameComponent(s) {
    return (s || '').replace(INVALID_FILENAME_CHARS, '_').slice(0, 100);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ──────────────────────────── IndexedDB helpers ────────────────────────────

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          db.createObjectStore(STORE_NAME, { keyPath: 'uuid' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbPut(record) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      tx.objectStore(STORE_NAME).put(record);
      tx.oncomplete = () => { db.close(); resolve(); };
      tx.onerror = () => { db.close(); reject(tx.error); };
    });
  }

  async function idbGetAll(uuids) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readonly');
      const store = tx.objectStore(STORE_NAME);
      const results = [];
      let remaining = uuids.length;
      if (remaining === 0) { db.close(); resolve([]); return; }
      for (const uuid of uuids) {
        const req = store.get(uuid);
        req.onsuccess = () => {
          if (req.result) results.push(req.result);
          if (--remaining === 0) { db.close(); resolve(results); }
        };
        req.onerror = () => {
          if (--remaining === 0) { db.close(); resolve(results); }
        };
      }
    });
  }

  async function idbClear() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      tx.objectStore(STORE_NAME).clear();
      tx.oncomplete = () => { db.close(); resolve(); };
      tx.onerror = () => { db.close(); reject(tx.error); };
    });
  }

  // ──────────────────────────── logging ────────────────────────────

  async function readState() {
    return new Promise((resolve) => {
      chrome.storage.local.get(['currentRun', 'lastRun', 'runLog'], (r) => resolve(r));
    });
  }

  async function writeState(patch) {
    return new Promise((resolve) => chrome.storage.local.set(patch, resolve));
  }

  async function appendLog(entry) {
    const { runLog = [] } = await readState();
    const next = runLog.concat([{ ts: new Date().toISOString(), ...entry }]);
    const capped = next.length > LOG_CAP ? next.slice(next.length - LOG_CAP) : next;
    await writeState({ runLog: capped });
    const method = entry.level === 'error' ? 'error' : entry.level === 'warn' ? 'warn' : 'log';
    console[method]('[Chronicle]', entry);
  }

  async function updateCurrentRun(patch) {
    const { currentRun } = await readState();
    if (!currentRun) return;
    await writeState({ currentRun: { ...currentRun, ...patch } });
  }

  // ──────────────────────────── fetch with backoff ────────────────────────────

  async function fetchJsonWithBackoff(url, label) {
    let lastErr = null;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const res = await fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
        if (res.ok) return await res.json();

        const retriable = res.status === 429 || (res.status >= 500 && res.status < 600);
        const bodyText = await res.text().catch(() => '');
        const snippet = bodyText.slice(0, 200);
        if (!retriable || attempt === MAX_RETRIES) {
          const err = new Error(`HTTP ${res.status}`);
          err.status = res.status;
          err.bodySnippet = snippet;
          throw err;
        }
        const wait = BACKOFF_SCHEDULE_MS[attempt];
        await appendLog({ level: 'warn', msg: label, status: res.status, retry: `${attempt + 1}/${MAX_RETRIES}`, bodySnippet: snippet });
        await sleep(wait);
      } catch (err) {
        lastErr = err;
        if (err.status !== undefined) throw err;
        if (attempt === MAX_RETRIES) break;
        const wait = BACKOFF_SCHEDULE_MS[attempt];
        await appendLog({ level: 'warn', msg: `${label}: ${err.message}`, retry: `${attempt + 1}/${MAX_RETRIES}` });
        await sleep(wait);
      }
    }
    throw lastErr || new Error('fetch failed');
  }

  // ──────────────────────────── API calls ────────────────────────────

  async function getOrgId() {
    const orgs = await fetchJsonWithBackoff('https://claude.ai/api/organizations', 'list organizations');
    if (!Array.isArray(orgs) || orgs.length === 0) throw new Error('No organizations returned by claude.ai. Make sure you are logged in.');
    const chatOrg = orgs.find((o) => Array.isArray(o.capabilities) && o.capabilities.includes('chat'));
    return (chatOrg || orgs[0]).uuid;
  }

  async function listProjects(orgId) {
    const url = `https://claude.ai/api/organizations/${orgId}/projects`;
    const projects = await fetchJsonWithBackoff(url, 'list projects');
    return Array.isArray(projects) ? projects : [];
  }

  async function listAllConversations(orgId) {
    const url = `https://claude.ai/api/organizations/${orgId}/chat_conversations`;
    const convs = await fetchJsonWithBackoff(url, 'list conversations');
    return Array.isArray(convs) ? convs : [];
  }

  async function fetchFullConversation(orgId, conversationUuid) {
    const url = `https://claude.ai/api/organizations/${orgId}/chat_conversations/${conversationUuid}?tree=True&rendering_mode=messages&render_all_tools=true`;
    return await fetchJsonWithBackoff(url, `fetch conversation ${conversationUuid}`);
  }

  // ──────────────────────────── export transform ────────────────────────────

  function normalizeMessages(fullConv) {
    const messages = [];
    const chatMessages = Array.isArray(fullConv.chat_messages) ? fullConv.chat_messages : [];
    for (const msg of chatMessages) {
      const content = [];
      if (Array.isArray(msg.content)) {
        for (const c of msg.content) {
          if (c && c.type === 'text' && typeof c.text === 'string') {
            content.push({ type: 'text', text: c.text });
          } else if (c && c.type === 'thinking' && typeof c.thinking === 'string') {
            content.push({ type: 'thinking', text: c.thinking });
          } else if (c && c.type === 'tool_use') {
            const block = { type: 'tool_use', tool_name: c.name || c.tool_name || null };
            if (c.input !== undefined) block.input = c.input;
            if (c.display_content) {
              const dc = c.display_content;
              if (dc.type === 'code_block') {
                content.push({
                  type: 'artifact',
                  artifact_id: c.id || null,
                  title: (dc.filename || '').split('/').pop() || null,
                  language: dc.language || null,
                  content: dc.code || ''
                });
                continue;
              } else if (dc.type === 'json_block' && dc.json_block) {
                try {
                  const parsed = JSON.parse(dc.json_block);
                  if (parsed && parsed.filename) {
                    content.push({
                      type: 'artifact',
                      artifact_id: c.id || null,
                      title: parsed.filename.split('/').pop() || null,
                      language: parsed.language || null,
                      content: parsed.code || ''
                    });
                    continue;
                  }
                } catch (_) { /* fall through to tool_use */ }
              }
            }
            content.push(block);
          }
        }
      } else if (typeof msg.text === 'string') {
        content.push({ type: 'text', text: msg.text });
      }

      messages.push({
        uuid: msg.uuid,
        sender: msg.sender,
        created_at: msg.created_at,
        content
      });
    }
    return messages;
  }

  function buildConversationRecord(fullConv, project) {
    return {
      uuid: fullConv.uuid,
      title: fullConv.name || null,
      created_at: fullConv.created_at,
      updated_at: fullConv.updated_at,
      project_uuid: project ? project.uuid : null,
      project_name: project ? project.name : null,
      model: fullConv.model || null,
      messages: normalizeMessages(fullConv)
    };
  }

  function countWords(record) {
    let total = 0;
    for (const msg of (record.messages || [])) {
      for (const block of (msg.content || [])) {
        if (block.text) {
          total += block.text.split(/\s+/).filter(Boolean).length;
        }
        if (block.content) {
          total += block.content.split(/\s+/).filter(Boolean).length;
        }
      }
    }
    return total;
  }

  // ──────────────────────────── date helpers ────────────────────────────

  function dateInRange(iso, startDate, endDate) {
    if (!iso) return false;
    const t = new Date(iso).getTime();
    const start = new Date(`${startDate}T00:00:00Z`).getTime();
    const end = new Date(`${endDate}T23:59:59.999Z`).getTime();
    return t >= start && t <= end;
  }

  function exportFilename(fetchAll, startDate, endDate) {
    const now = new Date();
    const ts = now.toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '-');
    if (fetchAll) {
      return `chronicle-export-all-${ts}.json`;
    }
    const timeOnly = now.toISOString().slice(11, 19).replace(/:/g, '');
    return `chronicle-export-${startDate}-to-${endDate}-${timeOnly}.json`;
  }

  function triggerDownload(jsonString, filename) {
    const safe = sanitizeFilenameComponent(filename) || 'chronicle-export.json';
    const blob = new Blob([jsonString], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = safe;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function isCancelled() {
    const { cancelRequested } = await new Promise((resolve) =>
      chrome.storage.local.get(['cancelRequested'], resolve)
    );
    return !!cancelRequested;
  }

  async function clearCancelFlag() {
    await new Promise((resolve) => chrome.storage.local.remove(['cancelRequested'], resolve));
  }

  // ──────────────────────────── phase 1: fetch conversations ────────────────────────────

  async function runFetch({ fetchAll, startDate, endDate }) {
    const startedAt = new Date().toISOString();
    await clearCancelFlag();
    await idbClear();
    await writeState({
      currentRun: { startedAt, phase: 'fetch', total: 0, completed: 0, fetchAll: !!fetchAll },
      runLog: [],
      fetchedMetas: []
    });
    const rangeLabel = fetchAll ? 'all conversations' : `${startDate} → ${endDate}`;
    await appendLog({ level: 'ok', msg: `Fetch started (${rangeLabel})` });

    try {
      const orgId = await getOrgId();
      await appendLog({ level: 'ok', msg: `Using organization ${orgId}` });

      const projects = await listProjects(orgId);
      const projectMap = new Map(projects.map((p) => [p.uuid, p]));
      await appendLog({ level: 'ok', msg: `Found ${projects.length} projects` });

      const allConvs = await listAllConversations(orgId);
      await appendLog({ level: 'ok', msg: `Found ${allConvs.length} conversations across all contexts` });

      const { seenConversations = {} } = await new Promise((resolve) =>
        chrome.storage.local.get(['seenConversations'], resolve)
      );

      // Detect deletions
      const currentUuidSet = new Set(allConvs.map((c) => c.uuid));
      const deletedUuids = Object.keys(seenConversations).filter((u) => !currentUuidSet.has(u));

      // Filter to candidates
      const toFetch = [];
      let skippedUnchanged = 0;
      for (const c of allConvs) {
        if (!fetchAll) {
          const field = c.updated_at;
          if (!dateInRange(field, startDate, endDate)) continue;
        }
        const prev = seenConversations[c.uuid];
        if (prev && c.updated_at && c.updated_at <= prev) {
          skippedUnchanged++;
          continue;
        }
        const project = c.project_uuid ? projectMap.get(c.project_uuid) || { uuid: c.project_uuid, name: '(unknown project)' } : null;
        toFetch.push({ project, stub: c });
      }

      if (skippedUnchanged > 0) {
        await appendLog({ level: 'ok', msg: `Skipping ${skippedUnchanged} unchanged conversations` });
      }
      if (deletedUuids.length > 0) {
        await appendLog({ level: 'warn', msg: `${deletedUuids.length} previously-seen conversations no longer exist` });
      }

      await updateCurrentRun({ total: toFetch.length });
      await appendLog({ level: 'ok', msg: `${toFetch.length} conversations to fetch` });

      const metas = [];
      let completed = 0;
      let succeeded = 0;
      let failed = 0;
      let cancelled = false;

      for (const { project, stub } of toFetch) {
        if (await isCancelled()) { cancelled = true; break; }
        try {
          const full = await fetchFullConversation(orgId, stub.uuid);
          const record = buildConversationRecord(full, project);
          const words = countWords(record);

          // Store full conversation in IndexedDB
          await idbPut(record);

          // Build meta for popup list
          const meta = {
            uuid: record.uuid,
            title: record.title || '(untitled)',
            date: (record.updated_at || '').slice(0, 10),
            words,
            changed: !!seenConversations[record.uuid]
          };
          metas.push(meta);

          // Push metas to storage so popup can render live
          await writeState({ fetchedMetas: [...metas] });

          succeeded++;
          await appendLog({
            level: 'ok',
            uuid: stub.uuid,
            project: project ? project.name : 'general',
            msg: stub.name || '(untitled)'
          });
        } catch (err) {
          failed++;
          await appendLog({
            level: 'error',
            uuid: stub.uuid,
            project: project ? project.name : 'general',
            msg: `${stub.name || '(untitled)'}: ${err.message}`,
            status: err.status || null,
            bodySnippet: err.bodySnippet || null
          });
        }
        completed++;
        await updateCurrentRun({ completed });
        await sleep(FETCH_DELAY_MS);
      }

      if (cancelled) await appendLog({ level: 'warn', msg: `Cancelled after ${completed}/${toFetch.length}` });

      // Store deletedUuids so download phase can include them in metadata
      const finishedAt = new Date().toISOString();
      const durationMs = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
      await writeState({
        currentRun: null,
        lastRun: {
          startedAt,
          finishedAt,
          durationMs,
          total: toFetch.length,
          succeeded,
          failed,
          skippedUnchanged,
          fetchDone: true,
          cancelled,
          deletedUuids,
          fetchAll: !!fetchAll,
          startDate,
          endDate
        }
      });
      await clearCancelFlag();
      const summary = `Fetch complete: ${succeeded} ok, ${failed} failed`;
      await appendLog({ level: cancelled ? 'warn' : 'ok', msg: cancelled ? `${summary} (cancelled)` : summary });

    } catch (err) {
      const finishedAt = new Date().toISOString();
      await appendLog({ level: 'error', msg: `Fetch aborted: ${err.message}`, status: err.status || null, bodySnippet: err.bodySnippet || null });
      await writeState({
        currentRun: null,
        lastRun: {
          startedAt,
          finishedAt,
          durationMs: new Date(finishedAt).getTime() - new Date(startedAt).getTime(),
          total: 0, succeeded: 0, failed: 0, aborted: true, error: err.message
        }
      });
      await clearCancelFlag();
    }
  }

  // ──────────────────────────── phase 2: download selected ────────────────────────────

  async function runDownload({ uuids, fetchAll, startDate, endDate }) {
    const startedAt = new Date().toISOString();
    await writeState({
      currentRun: { startedAt, phase: 'download' }
    });
    await appendLog({ level: 'ok', msg: `Building download for ${uuids.length} conversations…` });

    try {
      // Read selected conversations from IndexedDB
      const records = await idbGetAll(uuids);

      if (records.length === 0) {
        await appendLog({ level: 'error', msg: 'No conversation data found. Try fetching again.' });
        await writeState({ currentRun: null });
        return;
      }

      // Get last run info for deletedUuids
      const { lastRun } = await new Promise((resolve) =>
        chrome.storage.local.get(['lastRun'], resolve)
      );
      const deletedUuids = (lastRun && lastRun.deletedUuids) || [];

      const outputFile = exportFilename(fetchAll, startDate, endDate);

      const exportObj = {
        export_metadata: {
          exported_at: new Date().toISOString(),
          range_start: fetchAll ? null : `${startDate}T00:00:00Z`,
          range_end: fetchAll ? null : `${endDate}T23:59:59Z`,
          range_field: 'updated_at',
          fetch_all: !!fetchAll,
          total_conversations: records.length,
          skipped_unchanged: (lastRun && lastRun.skippedUnchanged) || 0,
          deleted_uuids: deletedUuids,
          cancelled: false,
          extension_version: EXTENSION_VERSION
        },
        conversations: records
      };

      triggerDownload(JSON.stringify(exportObj, null, 2), outputFile);

      // Update seenConversations
      const { seenConversations = {} } = await new Promise((resolve) =>
        chrome.storage.local.get(['seenConversations'], resolve)
      );
      const mergedSeen = { ...seenConversations };
      for (const r of records) {
        mergedSeen[r.uuid] = r.updated_at || null;
      }
      for (const u of deletedUuids) delete mergedSeen[u];
      await writeState({ seenConversations: mergedSeen });

      const finishedAt = new Date().toISOString();
      const durationMs = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
      await writeState({
        currentRun: null,
        lastRun: {
          startedAt,
          finishedAt,
          durationMs,
          total: records.length,
          succeeded: records.length,
          failed: 0,
          outputFile,
          downloaded: true
        }
      });
      await appendLog({ level: 'ok', msg: `Downloaded ${records.length} conversations → ${outputFile}` });

      // Clean up IndexedDB after successful download
      await idbClear();
      await new Promise((resolve) => chrome.storage.local.remove(['fetchedMetas'], resolve));

    } catch (err) {
      await appendLog({ level: 'error', msg: `Download failed: ${err.message}` });
      await writeState({ currentRun: null });
    }
  }

  // ──────────────────────────── legacy export (kept for backward compat) ────────────────────────────

  async function runExport({ fetchAll, startDate, endDate, rangeField, onlyUuids }) {
    const startedAt = new Date().toISOString();
    await clearCancelFlag();
    await writeState({
      currentRun: {
        startedAt,
        total: 0,
        completed: 0,
        rangeStart: fetchAll ? null : startDate,
        rangeEnd: fetchAll ? null : endDate,
        rangeField,
        fetchAll: !!fetchAll
      },
      runLog: []
    });
    const rangeLabel = fetchAll ? 'all conversations' : `${rangeField} ${startDate} → ${endDate}`;
    await appendLog({ level: 'ok', msg: `Export started (${rangeLabel})` });

    try {
      const orgId = await getOrgId();
      await appendLog({ level: 'ok', msg: `Using organization ${orgId}` });

      const projects = await listProjects(orgId);
      const projectMap = new Map(projects.map((p) => [p.uuid, p]));
      await appendLog({ level: 'ok', msg: `Found ${projects.length} projects` });

      const allConvs = await listAllConversations(orgId);
      await appendLog({ level: 'ok', msg: `Found ${allConvs.length} conversations across all contexts` });

      const { seenConversations = {} } = await new Promise((resolve) =>
        chrome.storage.local.get(['seenConversations'], resolve)
      );

      const currentUuidSet = new Set(allConvs.map((c) => c.uuid));
      const deletedUuids = Object.keys(seenConversations).filter((u) => !currentUuidSet.has(u));

      const toFetch = [];
      let skippedUnchanged = 0;
      for (const c of allConvs) {
        if (onlyUuids) {
          if (!onlyUuids.includes(c.uuid)) continue;
        } else {
          if (!fetchAll) {
            const field = c[rangeField];
            if (!dateInRange(field, startDate, endDate)) continue;
          }
          const prev = seenConversations[c.uuid];
          if (prev && c.updated_at && c.updated_at <= prev) {
            skippedUnchanged++;
            continue;
          }
        }
        const project = c.project_uuid ? projectMap.get(c.project_uuid) || { uuid: c.project_uuid, name: '(unknown project)' } : null;
        toFetch.push({ project, stub: c });
      }

      if (skippedUnchanged > 0) {
        await appendLog({ level: 'ok', msg: `Skipping ${skippedUnchanged} unchanged conversations (already up to date)` });
      }
      if (deletedUuids.length > 0) {
        await appendLog({ level: 'warn', msg: `${deletedUuids.length} previously-seen conversations no longer exist on claude.ai` });
      }

      await updateCurrentRun({ total: toFetch.length });
      await appendLog({ level: 'ok', msg: `Selected ${toFetch.length} conversations to fetch` });

      const records = [];
      const seenUpdates = {};
      let completed = 0;
      let succeeded = 0;
      let failed = 0;
      let cancelled = false;
      for (const { project, stub } of toFetch) {
        if (await isCancelled()) { cancelled = true; break; }
        try {
          const full = await fetchFullConversation(orgId, stub.uuid);
          const record = buildConversationRecord(full, project);
          records.push(record);
          seenUpdates[record.uuid] = record.updated_at || stub.updated_at || null;
          succeeded++;
          await appendLog({
            level: 'ok',
            uuid: stub.uuid,
            project: project ? project.name : 'general',
            msg: stub.name || '(untitled)'
          });
        } catch (err) {
          failed++;
          await appendLog({
            level: 'error',
            uuid: stub.uuid,
            project: project ? project.name : 'general',
            msg: `${stub.name || '(untitled)'}: ${err.message}`,
            status: err.status || null,
            bodySnippet: err.bodySnippet || null
          });
        }
        completed++;
        await updateCurrentRun({ completed });
        await sleep(FETCH_DELAY_MS);
      }

      if (cancelled) await appendLog({ level: 'warn', msg: `Cancelled after ${completed}/${toFetch.length}` });

      const outputFile = exportFilename(fetchAll, startDate, endDate);

      const exportObj = {
        export_metadata: {
          exported_at: new Date().toISOString(),
          range_start: fetchAll ? null : `${startDate}T00:00:00Z`,
          range_end: fetchAll ? null : `${endDate}T23:59:59Z`,
          range_field: rangeField,
          fetch_all: !!fetchAll,
          total_conversations: records.length,
          skipped_unchanged: skippedUnchanged,
          deleted_uuids: deletedUuids,
          cancelled,
          extension_version: EXTENSION_VERSION
        },
        conversations: records
      };

      const noChanges = records.length === 0 && deletedUuids.length === 0 && !cancelled;
      if (!noChanges) {
        triggerDownload(JSON.stringify(exportObj, null, 2), outputFile);
      }

      const mergedSeen = { ...seenConversations, ...seenUpdates };
      for (const u of deletedUuids) delete mergedSeen[u];
      await writeState({ seenConversations: mergedSeen });

      const finishedAt = new Date().toISOString();
      const durationMs = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
      await writeState({
        currentRun: null,
        lastRun: {
          startedAt,
          finishedAt,
          durationMs,
          total: toFetch.length,
          succeeded,
          failed,
          deletedCount: deletedUuids.length,
          outputFile: noChanges ? null : outputFile,
          rangeStart: fetchAll ? null : startDate,
          rangeEnd: fetchAll ? null : endDate,
          rangeField,
          fetchAll: !!fetchAll,
          noChanges,
          cancelled
        }
      });
      await clearCancelFlag();
      const summary = noChanges
        ? 'Nothing new since last run.'
        : `Export finished: ${succeeded} ok, ${failed} failed${deletedUuids.length ? `, ${deletedUuids.length} deleted` : ''} → ${outputFile}`;
      await appendLog({ level: cancelled ? 'warn' : 'ok', msg: cancelled ? `${summary} (cancelled)` : summary });
    } catch (err) {
      const finishedAt = new Date().toISOString();
      await appendLog({ level: 'error', msg: `Export aborted: ${err.message}`, status: err.status || null, bodySnippet: err.bodySnippet || null });
      await writeState({
        currentRun: null,
        lastRun: {
          startedAt,
          finishedAt,
          durationMs: new Date(finishedAt).getTime() - new Date(startedAt).getTime(),
          total: 0, succeeded: 0, failed: 0, outputFile: null, aborted: true, error: err.message
        }
      });
      await clearCancelFlag();
    }
  }

  // ──────────────────────────── message dispatch ────────────────────────────

  chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === 'fetchConversations') {
      runFetch({
        fetchAll: !!request.fetchAll,
        startDate: request.startDate,
        endDate: request.endDate
      }).catch((err) => console.error('[Chronicle] unhandled error', err));
      sendResponse({ accepted: true });
      return true;
    }
    if (request.action === 'downloadSelected') {
      runDownload({
        uuids: request.uuids,
        fetchAll: !!request.fetchAll,
        startDate: request.startDate,
        endDate: request.endDate
      }).catch((err) => console.error('[Chronicle] unhandled error', err));
      sendResponse({ accepted: true });
      return true;
    }
    // Legacy: direct export (for backward compat or programmatic use)
    if (request.action === 'exportRange') {
      const onlyUuids = Array.isArray(request.onlyUuids)
        ? request.onlyUuids
        : Array.isArray(request.retryUuids) ? request.retryUuids : null;
      runExport({
        fetchAll: !!request.fetchAll,
        startDate: request.startDate,
        endDate: request.endDate,
        rangeField: request.rangeField === 'created_at' ? 'created_at' : 'updated_at',
        onlyUuids
      }).catch((err) => console.error('[Chronicle] unhandled error', err));
      sendResponse({ accepted: true });
      return true;
    }
    if (request.action === 'ping') {
      sendResponse({ ok: true, version: EXTENSION_VERSION });
      return true;
    }
  });

  console.log('[Chronicle] content script loaded');
}
