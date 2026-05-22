const FIRST_PAGE_SIZE = 50;  // Erste Seite klein → sofort sichtbar
const PAGE_SIZE = 500;
const MAX_AUTO_LOAD = 1500; // Automatisch bis zu dieser Anzahl laden

// ── Zoom ──────────────────────────────────────────────────────
const ZOOM_LEVELS = [0.75, 1.0, 1.25, 1.5];
const DEFAULT_ZOOM = 1.25;
let _iframeZoom = DEFAULT_ZOOM;
let _activeIframe = null;
let _activeIframeBaseHtml = null;     // srcdoc nach CID-Ersatz + Block, ohne Zoom-CSS (Quelle für Zoom-Re-Render)
let _activeIframeOriginalHtml = null; // srcdoc nach CID-Ersatz, OHNE Block — Restore-Snapshot für „Bilder laden"

function _withZoom(html) {
  const style = `<style>html,body{zoom:${_iframeZoom}}</style>`;
  return html.includes('</head>') ? html.replace('</head>', style + '</head>') : style + html;
}

function _applyZoom() {
  const label = document.querySelector('#btn-zoom .zoom-control-value');
  if (label) label.textContent = Math.round(_iframeZoom * 100) + '%';
  if (_activeIframe && _activeIframeBaseHtml) {
    _activeIframe.srcdoc = _withZoom(_activeIframeBaseHtml);
  } else {
    // Plain-Text-E-Mail
    const body = document.getElementById('detail-body');
    if (body) body.style.zoom = _iframeZoom;
  }
}

// ── Folder-Cache ─────────────────────────────────────────────
const _folderCache = {};
const FOLDER_CACHE_TTL = 3 * 60 * 1000; // 3 Minuten

function _cacheKey() {
  const f = state.activeFolder === 'Sent' ? state.sentFilter : state.readFilter;
  return `${state.activeAccount}|${state.activeFolder}|${f}|${state.groupMode}`;
}
function _saveToCache() {
  if (state.searchQuery) return;
  _folderCache[_cacheKey()] = {
    emails: [...state.emails],
    totalItems: state.totalItems,
    threadCount: { ...state.threadCount },
    threadFirstSeen: { ...state.threadFirstSeen },
    ts: Date.now(),
  };
}
function _getFromCache() {
  if (state.searchQuery) return null;
  const key = _cacheKey();
  const entry = _folderCache[key];
  if (!entry) return null;
  if (Date.now() - entry.ts > FOLDER_CACHE_TTL) { delete _folderCache[key]; return null; }
  return entry;
}
function _invalidateFolderCache(accountId, folder) {
  Object.keys(_folderCache).forEach(k => {
    if (k.startsWith(`${accountId}|${folder}|`)) delete _folderCache[k];
  });
}

const STANDARD_FOLDER_ORDER = ['INBOX', 'Sent', 'Drafts', 'Trash', 'Spam'];

const KI_REFINE_ACTIONS = [
  { label: 'Kürzer',            instruction: 'Mache die Antwort kürzer und prägnanter.' },
  { label: 'Ausführlicher',     instruction: 'Mache die Antwort ausführlicher und detaillierter.' },
  { label: '+ Persönlicher Gruß', instruction: 'Füge einen persönlichen, herzlichen Gruß hinzu.' },
  { label: 'Sachlicher',        instruction: 'Formuliere die Antwort sachlicher und professioneller.' },
  { label: 'Herzlicher',        instruction: 'Formuliere die Antwort herzlicher und freundlicher.' },
];
const FOLDER_DISPLAY_NAMES = {
  'INBOX':  'Posteingang',
  'Sent':   'Gesendet',
  'Drafts': 'Entwürfe',
  'Trash':  'Papierkorb',
  'Spam':   'Spam',
};

function folderDisplayName(email_folder, display_name) {
  // email_folder ist der normierte Name (z.B. "Drafts"), display_name der letzte IMAP-Segment
  return FOLDER_DISPLAY_NAMES[email_folder] || display_name || email_folder;
}

function sortFolders(folders) {
  return [...folders].sort((a, b) => {
    const aKey = a.email_folder || a.imap_path;
    const bKey = b.email_folder || b.imap_path;
    const ia = STANDARD_FOLDER_ORDER.indexOf(aKey);
    const ib = STANDARD_FOLDER_ORDER.indexOf(bKey);
    if (ia >= 0 && ib >= 0) return ia - ib;
    if (ia >= 0) return -1;
    if (ib >= 0) return 1;
    // Custom-Ordner: nach imap_path sortieren (gruppiert Unterordner automatisch)
    return (a.imap_path || '').localeCompare(b.imap_path || '', 'de');
  });
}

function detectDelimiter(folders) {
  const slashes = folders.filter(f => f.imap_path && f.imap_path.includes('/')).length;
  const dots    = folders.filter(f => f.imap_path && f.imap_path.includes('.')).length;
  if (slashes > dots) return '/';
  if (dots > 0) return '.';
  return null;
}

function folderVisualDepth(imap_path, delimiter) {
  if (!delimiter || !imap_path) return 0;
  const segments = imap_path.split(delimiter);
  // INBOX ist immer die implizite Wurzel → Tiefe = Segmente nach INBOX
  if (segments[0] === 'INBOX') return Math.max(0, segments.length - 2);
  return Math.max(0, segments.length - 1);
}

function buildFolderTree(folders, delimiter) {
  // Fehlende Zwischenordner als virtuelle (nicht-klickbare) Platzhalter einfügen,
  // damit keine Tiefensprünge in der Hierarchie entstehen.
  if (!delimiter) return folders;
  const knownPaths = new Set(folders.map(f => f.imap_path));
  const virtual = [];
  folders.forEach(f => {
    const parts = f.imap_path.split(delimiter);
    for (let i = 1; i < parts.length; i++) {
      const ancestorPath = parts.slice(0, i).join(delimiter);
      if (!knownPaths.has(ancestorPath)) {
        virtual.push({
          imap_path:    ancestorPath,
          email_folder: null,            // null → nicht anklickbar
          display_name: parts[i - 1],
          _virtual:     true,
        });
        knownPaths.add(ancestorPath);
      }
    }
  });
  return [...folders, ...virtual];
}

let state = {
  accounts: [],
  categories: [],        // [{slug, name, description}] — dynamisch geladen
  folders: {},          // accountId → [{imap_path, display_name, unread_count, …}]
  delimiters: {},       // accountId → '/' | '.' | null
  collapsedFolders: new Set(JSON.parse(localStorage.getItem('mf_collapsed') || '[]')),
  smtpServers: [],
  sentToday: { counts: {}, limit: 10000 },  // Tagesversand pro Account, refreshed nach jedem send-result
  activeAccount: null,
  activeFolder: 'INBOX',
  groupMode: 'thread',   // 'thread' | 'sender'
  readFilter: 'all',     // 'all' | 'unread' | 'read' — Posteingang etc.
  sentFilter: 'all',     // 'all' | 'webhook' | 'normal' — nur im Sent-Ordner aktiv
  newCount: 0,           // is_new=true Zähler — für Tab-Badge
  searchQuery: '',
  emails: [],
  page: 1,
  totalItems: 0,
  loadingMore: false,
  allLoaded: false,
  activeEmailId: null,
  threadCount: {},      // display_thread_id → Anzahl geladener E-Mails
  threadFirstSeen: {},  // display_thread_id → true nach erstem Vorkommen
  selectedEmails: new Set(), // IDs der markierten E-Mails (Mehrfachauswahl)
  lastClickedEl: null,       // DOM-Element des letzten Klicks (Shift+Click-Anker)
  kiModeActive: false,
  kiCategoryFilter: '',      // '' = alle anzeigen
  _currentReplyOpts: null,   // Compose-Optionen der aktuell geöffneten E-Mail (für KI-Sidebar)
};

async function init() {
  if (!await auth.authRefresh()) return;
  await Promise.all([loadAccounts(), loadSmtpServers(), loadCategories()]);
  await loadEmails(true);
  setupInfiniteScroll();
  setupViewToggle();
  setupReadFilter();
  setupSearch();
  setupComposeToolbar();
  setupSpamRules();
  loadSpamRulesCount();
  startAutoRefresh();
  startEventSource();
}

async function loadCategories() {
  try {
    const cats = await api.getCategories();
    state.categories = cats;
    _renderCategoryButtons();
  } catch (e) {
    // Fallback: eingebaute Defaults
    state.categories = [
      { slug: 'focus',      name: 'Fokus'  },
      { slug: 'quick-reply',name: 'Quick'  },
      { slug: 'office',     name: 'Office' },
      { slug: 'info-trash', name: 'Info'   },
    ];
    _renderCategoryButtons();
  }
}

function _renderCategoryButtons() {
  // KI-Toolbar-Filterbuttons
  const filterGroup = document.getElementById('ki-filter-group');
  if (filterGroup) {
    filterGroup.innerHTML = '<button class="ki-filter-btn active" data-filter="">Alle</button>';
    state.categories.forEach(cat => {
      const btn = document.createElement('button');
      btn.className = 'ki-filter-btn';
      btn.dataset.filter = cat.slug;
      btn.textContent = cat.name;
      filterGroup.appendChild(btn);
    });
  }

  // Detail-Panel Kategorie-Buttons
  const catGroup = document.getElementById('ki-cat-group');
  if (catGroup) {
    catGroup.innerHTML = '';
    state.categories.forEach(cat => {
      const btn = document.createElement('button');
      btn.className = 'ki-cat-btn';
      btn.dataset.cat = cat.slug;
      btn.textContent = cat.name;
      catGroup.appendChild(btn);
    });
  }
}

let _lastKnownSync = null;

function startAutoRefresh() {
  setInterval(async () => {
    try {
      const status = await api.getSyncStatus();
      if (!status.last_sync) return;
      if (status.last_sync === _lastKnownSync) return;
      _lastKnownSync = status.last_sync;
      await silentRefresh();
    } catch (e) {
      // Polling-Fehler still ignorieren
    }
  }, 120_000);
}

// startEventSource() liegt in js/sse.js (C4 Phase 2).

// _addSendNotif, _handleSendResult, _sendNotifContainer liegen in js/compose.js (C4 Phase 2 / A).

function _escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// _linkifyHtml() liegt in js/compose.js (C4 Phase 2 / A).

// ── KI-Modus ────────────────────────────────────────────────

function toggleKiMode() {
  state.kiModeActive = !state.kiModeActive;
  const toolbar = document.getElementById('ki-toolbar');
  const btn     = document.getElementById('btn-ki-mode');
  toolbar.style.display = state.kiModeActive ? 'flex' : 'none';
  btn.textContent = state.kiModeActive ? 'KI-Modus aktiv' : 'KI-Modus';
  btn.classList.toggle('ki-active', state.kiModeActive);
  document.getElementById('topbar').classList.toggle('ki-open', state.kiModeActive);
  document.getElementById('app').classList.toggle('ki-open', state.kiModeActive);

  if (!state.kiModeActive) {
    // KI-Elemente ausblenden wenn Modus verlassen wird
    document.getElementById('btn-ki-suggest').style.display = 'none';
    document.getElementById('btn-ki-analyze').style.display = 'none';
    document.getElementById('detail-ki-bar').style.display = 'none';
    closeKiAnalyzeSidebar();
    if (state.kiCategoryFilter) {
      state.kiCategoryFilter = '';
      document.querySelectorAll('.ki-filter-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.filter === ''));
    }
  } else if (state.activeEmailId) {
    // KI-Modus aktiviert mit bereits geöffneter E-Mail → neu öffnen damit KI-Elemente erscheinen
    const email  = state.emails.find(e => e.id === state.activeEmailId);
    const itemEl = document.querySelector('.email-item.active');
    if (email && itemEl) openEmail(email, itemEl);
  }

  // Liste neu rendern, damit Badges erscheinen/verschwinden
  renderEmails(true);
}

async function runKiTriage() {
  const statusEl = document.getElementById('ki-triage-status');
  const btn      = document.getElementById('btn-ki-triage');
  statusEl.textContent = 'Kategorisiere…';
  btn.disabled = true;
  try {
    const result = await api.ai.triage(state.activeAccount, state.activeFolder);
    statusEl.textContent = `${result.categorized} E-Mails kategorisiert`;
    await loadEmails(true);
  } catch (e) {
    statusEl.textContent = 'Fehler: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

function renderKiRefineBar() {
  const bar = document.getElementById('ki-refine-bar');
  if (!state.kiModeActive) {
    bar.style.display = 'none';
    return;
  }
  bar.innerHTML = KI_REFINE_ACTIONS.map((a, i) =>
    `<button class="ki-refine-btn" data-ki-refine="${i}">${escHtml(a.label)}</button>`
  ).join('');
  bar.style.display = 'flex';

  bar.querySelectorAll('.ki-refine-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const action = KI_REFINE_ACTIONS[+btn.dataset.kiRefine];
      if (!action) return;
      const bodyEl = document.getElementById('ci-body');
      const currentText = bodyEl.innerText || bodyEl.textContent || '';
      btn.disabled = true;
      const origLabel = btn.textContent;
      btn.textContent = '…';
      try {
        const result = await api.ai.refine(currentText, action.instruction);
        bodyEl.innerHTML = escHtml(result.text).replace(/\n/g, '<br>');
      } catch (e) {
        // Fehler still ignorieren, Button bleibt klickbar
      } finally {
        btn.disabled = false;
        btn.textContent = origLabel;
      }
    });
  });
}

// ─────────────────────────────────────────────────────────────

let _refreshing = false;
async function silentRefresh() {
  if (_refreshing) return;
  if (state.searchQuery) { loadUnreadCounts(); return; }
  _refreshing = true;
  try {
    const params = { page: 1, limit: PAGE_SIZE };
    if (state.activeAccount) params.account = state.activeAccount;
    if (state.activeFolder)  params.folder  = state.activeFolder;

    const fetchFn = isFlatFolder()
      ? fetchFlatEmails
      : (state.groupMode === 'sender'
          ? api.getEmailsBySender.bind(api)
          : api.getThreadedEmails.bind(api));

    const data = await fetchFn(params);
    const fresh = data.items || [];
    const knownById = Object.fromEntries(state.emails.map(e => [e.id, e]));
    const newEmails = fresh.filter(e => !knownById[e.id]);

    // Flag-Änderungen auf bereits geladenen E-Mails anwenden
    fresh.forEach(e => {
      const local = knownById[e.id];
      if (!local) return;
      const el = document.querySelector(`.email-item[data-id="${e.id}"]`);
      if (local.is_read !== e.is_read) {
        local.is_read = e.is_read;
        if (el) {
          el.classList.toggle('unread', !e.is_read);
          const btn = el.querySelector('.flag-read-toggle');
          if (btn) {
            btn.classList.toggle('unread', !e.is_read);
            btn.title = e.is_read ? 'Als ungelesen markieren' : 'Als gelesen markieren';
          }
        }
      }
      if (local.is_flagged !== e.is_flagged) {
        local.is_flagged = e.is_flagged;
      }
      if (local.is_answered !== e.is_answered) {
        local.is_answered = e.is_answered;
        if (el) el.querySelector('.flag-answered')?.classList.toggle('active', !!e.is_answered);
      }
      // ai_category vom Server übernehmen; wenn sie sich geändert hat und KI-Filter aktiv ist,
      // E-Mail aus DOM entfernen falls sie nicht mehr in die Kategorie passt
      if (local.ai_category !== e.ai_category) {
        local.ai_category = e.ai_category;
        if (state.kiCategoryFilter && el) {
          const hidden = e.ai_category !== state.kiCategoryFilter || e.is_read;
          if (hidden) el.remove();
        }
      }
    });

    // Verschobene / gelöschte E-Mails aus der UI entfernen:
    // Wenn fresh < PAGE_SIZE Einträge hat, kennt der Server alle aktuellen E-Mails im
    // sichtbaren Zeitraum. Fehlende IDs → aus State und DOM entfernen.
    // Bei frisch >= PAGE_SIZE reicht der Vergleich nur für den Datumsbereich von fresh.
    const freshIds = new Set(fresh.map(e => e.id));
    const oldestFreshDate = fresh.length > 0
      ? (fresh[fresh.length - 1].date_sent || '')
      : '';
    const removedEmails = state.emails.filter(e =>
      !freshIds.has(e.id) &&
      (fresh.length < PAGE_SIZE || (e.date_sent || '') >= oldestFreshDate)
    );
    if (removedEmails.length > 0) {
      const removedIds = new Set(removedEmails.map(e => e.id));
      state.emails = state.emails.filter(e => !removedIds.has(e.id));
      removedEmails.forEach(e => {
        const el = document.querySelector(`.email-item[data-id="${e.id}"]`);
        if (el) el.remove();
        // Detail-Panel leeren falls gerade diese E-Mail geöffnet ist
        if (state.activeEmailId === e.id) {
          state.activeEmailId = null;
          showEmpty();
        }
      });
      cleanupThreadStyling();
    }

    if (newEmails.length === 0) {
      loadUnreadCounts();
      return;
    }

    // Neue E-Mails oben einfügen
    state.emails = newEmails.concat(state.emails);
    newEmails.forEach(email => {
      const tid = getThreadId(email);
      state.threadCount[tid] = (state.threadCount[tid] || 0) + 1;
    });

    const listEl = document.getElementById('email-list');
    const atTop = listEl.scrollTop < 50;

    // DOM-Elemente oben einfügen (KI-Filter + readFilter berücksichtigen)
    const fragment = document.createDocumentFragment();
    newEmails.forEach((email, idx) => {
      if (state.kiCategoryFilter && (email.ai_category !== state.kiCategoryFilter || email.is_read)) return;
      if (state.readFilter === 'unread' && email.is_read) return;
      if (state.readFilter === 'read'   && !email.is_read) return;
      const el = buildEmailItem(email, idx, newEmails);
      el.classList.add('email-new');
      fragment.appendChild(el);
    });
    listEl.prepend(fragment);

    // Animation nach kurzem Delay entfernen
    setTimeout(() => {
      listEl.querySelectorAll('.email-new').forEach(el => el.classList.remove('email-new'));
    }, 1000);

    if (atTop) listEl.scrollTop = 0;
    loadUnreadCounts();
    _saveToCache();
  } catch (e) {
    console.error('silentRefresh:', e);
  } finally {
    _refreshing = false;
  }
}

async function loadSmtpServers() {
  try {
    const data = await api.getSmtpServers();
    state.smtpServers = data.items || [];
  } catch (e) {
    console.error('loadSmtpServers:', e);
  }
}

function setupViewToggle() {
  document.getElementById('btn-view-thread').addEventListener('click', () => {
    if (state.groupMode === 'thread') return;
    state.groupMode = 'thread';
    document.getElementById('btn-view-thread').classList.add('active');
    document.getElementById('btn-view-sender').classList.remove('active');
    loadEmails(true);
  });
  document.getElementById('btn-view-sender').addEventListener('click', () => {
    if (state.groupMode === 'sender') return;
    state.groupMode = 'sender';
    document.getElementById('btn-view-sender').classList.add('active');
    document.getElementById('btn-view-thread').classList.remove('active');
    loadEmails(true);
  });
}

// Buttons je nach aktivem Ordner umschriften.
// Posteingang & co: Alle / Ungelesen / Gelesen.
// Sent: Alle / Webhook / Normal (Filter auf emails.webhook).
function renderReadFilterButtons() {
  const container = document.querySelector('.read-filter');
  if (!container) return;
  const isSent = state.activeFolder === 'Sent';
  const active = isSent ? state.sentFilter : state.readFilter;
  const buttons = isSent
    ? [['all', 'Alle'], ['webhook', 'Webhook'], ['normal', 'Normal']]
    : [['all', 'Alle'], ['unread', 'Ungelesen'], ['read', 'Gelesen']];
  container.innerHTML = buttons
    .map(([f, label]) =>
      `<button class="read-filter-btn${f === active ? ' active' : ''}" data-filter="${f}">${label}</button>`,
    )
    .join('');
}

function setupReadFilter() {
  renderReadFilterButtons();
  const container = document.querySelector('.read-filter');
  if (!container) return;
  // Event-Delegation, weil die Buttons je nach Ordner neu gerendert werden.
  container.addEventListener('click', (e) => {
    const btn = e.target.closest('.read-filter-btn');
    if (!btn) return;
    const filter = btn.dataset.filter;
    const isSent = state.activeFolder === 'Sent';
    const currentKey = isSent ? 'sentFilter' : 'readFilter';
    if (filter === state[currentKey]) return;
    state[currentKey] = filter;
    container.querySelectorAll('.read-filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    loadEmails(true);
  });
}

function syncFolderActive() {
  document.querySelectorAll('.folder-item[data-folder]').forEach(el => {
    if (state.searchQuery) {
      el.classList.remove('active');
    } else {
      el.classList.toggle('active',
        el.dataset.account === state.activeAccount &&
        el.dataset.folder  === state.activeFolder
      );
    }
  });
}

function setupSearch() {
  const input = document.getElementById('search-input');
  const clearBtn = document.getElementById('search-clear');

  // Suche nur bei Enter
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const q = input.value.trim();
      if (q === state.searchQuery) return;
      state.searchQuery = q;
      syncFolderActive();
      loadEmails(true);
    }
    if (e.key === 'Escape') {
      input.value = '';
      state.searchQuery = '';
      clearBtn.style.display = 'none';
      input.blur();
      syncFolderActive();
      loadEmails(true);
    }
  });

  clearBtn.addEventListener('mousedown', (e) => {
    // mousedown statt click verhindert, dass blur auf dem Input zuerst feuert
    e.preventDefault();
    input.value = '';
    state.searchQuery = '';
    clearBtn.style.display = 'none';
    input.blur();
    syncFolderActive();
    loadEmails(true);
  });

  input.addEventListener('input', () => {
    clearBtn.style.display = input.value ? 'block' : 'none';
  });
}

async function loadAccounts() {
  try {
    const data = await api.getAccounts();
    state.accounts = (data.items || []).sort((a, b) => a.name.localeCompare(b.name, 'de'));
    if (state.accounts.length > 0 && !state.activeAccount) {
      state.activeAccount = state.accounts[0].id;
    }
    await loadAllFolders();
    renderSidebar();
    loadUnreadCounts();
    refreshSentToday();
  } catch (e) {
    console.error('loadAccounts:', e);
  }
}

async function loadAllFolders() {
  try {
    await Promise.all(state.accounts.map(async (account) => {
      const data = await api.getFolders(account.id);
      const seen = new Set();
      const items = (data.items || []).filter(f => {
        if (seen.has(f.imap_path)) return false;
        seen.add(f.imap_path);
        return true;
      });
      state.folders[account.id]   = items;
      state.delimiters[account.id] = detectDelimiter(items);
    }));
  } catch (e) {
    console.error('loadAllFolders:', e);
  }
}

function isDescendantOf(path, ancestorPath, delimiter) {
  if (!delimiter) return false;
  return path.startsWith(ancestorPath + delimiter);
}

function toggleFolderCollapse(imapPath) {
  if (state.collapsedFolders.has(imapPath)) {
    state.collapsedFolders.delete(imapPath);
  } else {
    state.collapsedFolders.add(imapPath);
  }
  localStorage.setItem('mf_collapsed', JSON.stringify([...state.collapsedFolders]));
  updateFolderVisibility();
}

function updateFolderVisibility() {
  document.querySelectorAll('.folder-item[data-imap-path]').forEach(item => {
    const path      = item.dataset.imapPath;
    const delimiter = state.delimiters[item.dataset.account];
    let hidden = false;
    for (const collapsed of state.collapsedFolders) {
      if (isDescendantOf(path, collapsed, delimiter)) { hidden = true; break; }
    }
    item.style.display = hidden ? 'none' : '';
  });
  document.querySelectorAll('.folder-toggle').forEach(arrow => {
    const path = arrow.closest('.folder-item').dataset.imapPath;
    arrow.classList.toggle('collapsed', state.collapsedFolders.has(path));
  });
}

async function loadUnreadCounts() {
  try {
    const data = await api.getFolderCounts();
    const folders = data.items || [];
    folders.forEach(f => {
      const folderKey = f.email_folder || f.imap_path;
      const el = document.querySelector(
        `.folder-item[data-account="${f.account}"][data-folder="${folderKey}"] .folder-count`
      );
      if (!el) return;
      if (f.unread_count > 0) {
        el.textContent = f.unread_count;
        el.style.display = '';
      } else {
        el.style.display = 'none';
      }
    });
    state.newCount = data.new_count ?? 0;
    _updateDocumentTitle();
  } catch (e) {
    console.error('loadUnreadCounts:', e);
  }
}

function _updateDocumentTitle() {
  const total = state.newCount;
  const capped = Math.min(total, 99);
  document.title = total > 0 ? `(${capped}) Mailflow` : 'Mailflow';
  _updateFaviconBadge(total);
  if ('setAppBadge' in navigator) {
    total > 0 ? navigator.setAppBadge(total) : navigator.clearAppBadge();
  }
}

function _updateFaviconBadge(total) {
  // Chrome zeigt bei angepinnten Tabs nur das Favicon (nicht den Titel). Darum
  // zeichnen wir den is_new-Zähler zusätzlich direkt ins Icon. Vivaldi nutzt
  // weiterhin den Titel-Badge aus _updateDocumentTitle().
  const link = document.querySelector('link[rel="icon"]');
  if (!link) return;

  if (!total) {
    link.href = 'favicon.svg';
    return;
  }

  const capped = Math.min(total, 99);
  const label = total > 99 ? '99+' : String(capped);
  const fontSize = label.length === 1 ? 25 : label.length === 2 ? 21 : 15;
  const textY = label.length === 1 ? 24 : label.length === 2 ? 23 : 22;
  const svg = `
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="7" fill="#0a84ff"/>
  <text x="16" y="${textY}" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="${fontSize}" font-weight="300" fill="white">${label}</text>
</svg>`.trim();
  link.href = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function _adjustFolderCount(accountId, folder, delta) {
  const el = document.querySelector(
    `.folder-item[data-account="${accountId}"][data-folder="${folder}"] .folder-count`
  );
  if (!el) return;
  const next = Math.max(0, (parseInt(el.textContent, 10) || 0) + delta);
  if (next > 0) {
    el.textContent = next;
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
  _updateDocumentTitle();
}

let _loadGen = 0;  // Jeder reset() erhöht den Zähler — veraltete Fetches werden verworfen

function _addEmailBatch(newEmails, isReset) {
  newEmails.forEach(email => {
    const tid = getThreadId(email);
    state.threadCount[tid] = (state.threadCount[tid] || 0) + 1;
    if (state.threadCount[tid] === 2) {
      const firstItem = document.querySelector(`.email-item[data-thread="${CSS.escape(tid)}"]`);
      if (firstItem) {
        firstItem.classList.add('thread-member', 'thread-first');
        firstItem.classList.remove('thread-last');
      }
    }
  });
  if (isReset) {
    state.emails = newEmails;
    renderEmails(true);
  } else {
    const prevLength = state.emails.length;
    state.emails = state.emails.concat(newEmails);
    appendEmails(newEmails, prevLength);
  }
}

async function loadEmails(reset = false) {
  if (reset) {
    // Cache-Treffer: sofort aus Speicher laden, kein API-Call
    const cached = _getFromCache();
    if (cached) {
      _loadGen++;
      state.emails        = cached.emails;
      state.totalItems    = cached.totalItems;
      state.threadCount   = cached.threadCount;
      state.threadFirstSeen = cached.threadFirstSeen;
      // allLoaded NICHT pauschal auf true setzen — sonst blockt Infinite-Scroll
      // nach einem Cache-Hit, obwohl im Ordner noch mehr Mails liegen.
      state.allLoaded     = cached.totalItems > 0 && cached.emails.length >= cached.totalItems;
      state.loadingMore   = false;
      state.page          = Math.ceil(cached.emails.length / PAGE_SIZE) + 1;
      state.activeEmailId = null;
      state.selectedEmails.clear();
      state.lastClickedEl = null;
      showEmpty();
      renderEmails(true);
      updateListHeader();
      updateFooter();
      loadUnreadCounts();
      return;
    }
    _loadGen++;
    state.emails = [];
    state.page = 1;
    state.allLoaded = false;
    state.loadingMore = false;
    state.activeEmailId = null;
    state.threadCount = {};
    state.threadFirstSeen = {};
    state.selectedEmails.clear();
    state.lastClickedEl = null;
    showEmpty();
    document.getElementById('email-list').innerHTML =
      '<div class="loading">Lade E-Mails…</div>';
  }

  if (state.loadingMore || state.allLoaded) return;
  state.loadingMore = true;
  const myGen = _loadGen;
  updateFooter();

  try {
    const baseParams = {};
    if (state.activeAccount) baseParams.account = state.activeAccount;
    if (state.activeFolder)  baseParams.folder  = state.activeFolder;
    if (state.activeFolder === 'Sent') {
      if (state.sentFilter === 'webhook') baseParams.webhook = 'true';
      if (state.sentFilter === 'normal')  baseParams.webhook = 'false';
    } else {
      if (state.readFilter === 'unread') baseParams.is_read = 'false';
      if (state.readFilter === 'read')   baseParams.is_read = 'true';
    }

    if (state.searchQuery) {
      // Suche ordnerübergreifend — kein folder-Parameter
      const searchParams = { q: state.searchQuery };
      if (state.activeAccount) searchParams.account = state.activeAccount;
      if (state.readFilter === 'unread') searchParams.is_read = 'false';
      if (state.readFilter === 'read')   searchParams.is_read = 'true';
      const data = await api.search(searchParams);
      if (myGen !== _loadGen) return;
      state.totalItems = data.totalItems || 0;
      state.allLoaded = true;
      _addEmailBatch(data.items || [], reset);
      updateListHeader();
    } else if (!reset) {
      // Infinite-Scroll: nächste Seite anhängen, NICHT die Stage-1/2/3-Logik
      // durchlaufen (die ersetzt sonst die Liste via _addEmailBatch(..., true)
      // und springt im Scroll nach oben).
      const fetchFn = isFlatFolder()
        ? fetchFlatEmails
        : (state.groupMode === 'sender'
            ? api.getEmailsBySender.bind(api)
            : api.getThreadedEmails.bind(api));
      const data = await fetchFn({ ...baseParams, page: state.page, limit: PAGE_SIZE });
      if (myGen !== _loadGen) return;
      _addEmailBatch(data.items || [], false);
      state.page += 1;
      if (!data.hasMore) state.allLoaded = true;
      updateListHeader();
    } else {
      const fetchFn = isFlatFolder()
        ? fetchFlatEmails
        : (state.groupMode === 'sender'
            ? api.getEmailsBySender.bind(api)
            : api.getThreadedEmails.bind(api));

      // Stage 1: erste 50 sofort anzeigen
      const quick = await fetchFn({ ...baseParams, page: 1, limit: FIRST_PAGE_SIZE });
      if (myGen !== _loadGen) return;

      state.totalItems = quick.totalItems || 0;
      _addEmailBatch(quick.items || [], reset);
      state.page = 2;
      updateListHeader();
      updateFooter();

      if (!quick.hasMore) {
        state.allLoaded = true;
      } else {
        // Stage 2: Seite 1 nochmal mit voller Größe laden (ersetzt die 50)
        const fullFirst = await fetchFn({ ...baseParams, page: 1, limit: PAGE_SIZE });
        if (myGen !== _loadGen) return;

        state.threadCount = {};
        state.threadFirstSeen = {};
        _addEmailBatch(fullFirst.items || [], true);
        state.page = 2;
        updateListHeader();
        updateFooter();

        // Restliche Seiten parallel laden
        if (fullFirst.hasMore && state.emails.length < MAX_AUTO_LOAD) {
          const toLoad = Math.min(MAX_AUTO_LOAD, state.totalItems);
          const pagesNeeded = Math.ceil((toLoad - state.emails.length) / PAGE_SIZE);
          const results = await Promise.all(
            Array.from({ length: pagesNeeded }, (_, i) =>
              fetchFn({ ...baseParams, page: state.page + i, limit: PAGE_SIZE })
            )
          );
          if (myGen !== _loadGen) return;

          results.forEach(pageData => {
            _addEmailBatch(pageData.items || [], false);
            if (!pageData.hasMore) state.allLoaded = true;
          });
          state.page += pagesNeeded;
        } else {
          state.allLoaded = !fullFirst.hasMore;
        }
      }
    }
    _saveToCache();
  } catch (e) {
    document.getElementById('email-list').innerHTML =
      '<div class="loading">Fehler beim Laden.</div>';
    console.error('loadEmails:', e);
  } finally {
    state.loadingMore = false;
    updateFooter();
  }
}

function setupInfiniteScroll() {
  const listEl = document.getElementById('email-list');
  listEl.addEventListener('scroll', () => {
    const nearBottom = listEl.scrollTop + listEl.clientHeight >= listEl.scrollHeight - 150;
    if (nearBottom && !state.loadingMore && !state.allLoaded) {
      loadEmails(false);
    }
  });
}

let _sentTodayTimer = null;
async function refreshSentToday() {
  try {
    const data = await api.getSentToday();
    state.sentToday.counts = data.counts || {};
    state.sentToday.limit = data.limit || 10000;
    document.querySelectorAll('.account-sent-counter').forEach(el => {
      const aid = el.dataset.account;
      const c = state.sentToday.counts[aid] || 0;
      const lim = state.sentToday.limit;
      el.textContent = `${c}/${lim.toLocaleString('de-DE')}`;
      el.classList.toggle('warn', c >= lim * 0.8);
      el.classList.toggle('over', c >= lim);
    });
  } catch (err) {
    console.warn('sent-today refresh failed', err);
  }
}
function scheduleSentTodayRefresh() {
  if (_sentTodayTimer) return;
  _sentTodayTimer = setTimeout(() => {
    _sentTodayTimer = null;
    refreshSentToday();
  }, 1500);
}

function renderSidebar() {
  const sidebar = document.getElementById('sidebar-accounts');
  sidebar.innerHTML = '';
  state.accounts.forEach(account => {
    const section = document.createElement('div');
    section.className = 'account-section';
    const label = document.createElement('div');
    label.className = 'account-label';
    const sent = state.sentToday.counts[account.id] || 0;
    const limit = state.sentToday.limit || 10000;
    label.innerHTML = `
      <span>${account.name || account.from_email}</span>
      <span class="account-sent-counter" title="Heute versendet (Mailbox.org-Tageslimit ${limit.toLocaleString('de-DE')})" data-account="${account.id}">${sent}/${limit.toLocaleString('de-DE')}</span>
      <button class="account-settings-btn" title="Einstellungen">⚙</button>
    `;
    label.querySelector('.account-settings-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      openAccountSettings(account);
    });
    section.appendChild(label);

    // Ordner aus state.folders laden, Fallback auf Standard-Ordner
    const accountFolders = state.folders[account.id] || [];
    let foldersToShow;
    if (accountFolders.length > 0) {
      foldersToShow = sortFolders(accountFolders);
    } else {
      // Fallback solange noch keine Ordner synchronisiert wurden
      foldersToShow = STANDARD_FOLDER_ORDER.map(p => ({
        imap_path: p, email_folder: p, display_name: FOLDER_DISPLAY_NAMES[p]
      }));
    }

    const delimiter  = state.delimiters[account.id] || detectDelimiter(accountFolders);
    const allFolders = sortFolders(buildFolderTree(foldersToShow, delimiter));

    // Welche imap_paths haben Kinder?
    const parentPaths = new Set(
      allFolders
        .filter(f => delimiter && allFolders.some(
          other => other.imap_path !== f.imap_path &&
                   other.imap_path.startsWith(f.imap_path + delimiter)
        ))
        .map(f => f.imap_path)
    );

    let separatorInserted = false;
    allFolders.forEach(f => {
      const emailFolder = f.email_folder || f.imap_path;
      const isVirtual  = !!f._virtual || !!f.no_select;
      const isStandard = STANDARD_FOLDER_ORDER.includes(emailFolder);

      // Trennlinie nach dem letzten Standard-Ordner, vor dem ersten Custom-Ordner
      if (!isStandard && !separatorInserted) {
        separatorInserted = true;
        const sep = document.createElement('div');
        sep.className = 'folder-separator';
        section.appendChild(sep);
      }
      const isActive   = !isVirtual && !state.searchQuery && state.activeAccount === account.id && state.activeFolder === emailFolder;
      const depth      = folderVisualDepth(f.imap_path, delimiter);
      const hasKids    = parentPaths.has(f.imap_path);
      const isCollapsed = state.collapsedFolders.has(f.imap_path);

      const item = document.createElement('div');
      item.className = 'folder-item'
        + (isActive  ? ' active'         : '')
        + (isVirtual ? ' folder-virtual' : '');
      item.dataset.account  = account.id;
      item.dataset.imapPath = f.imap_path;
      if (!isVirtual) item.dataset.folder = emailFolder;
      if (depth > 0)  item.style.paddingLeft = `${28 + depth * 14}px`;

      // Pfeil-Icon + Name + Badge
      const arrowHtml = hasKids
        ? `<span class="folder-toggle${isCollapsed ? ' collapsed' : ''}">▾</span>`
        : `<span class="folder-toggle-gap"></span>`;
      const name = isVirtual
        ? escHtml(f.display_name)
        : escHtml(folderDisplayName(emailFolder, f.display_name));
      item.innerHTML = arrowHtml
        + `<span class="folder-name">${name}</span>`
        + (isVirtual ? '' : '<span class="folder-count" style="display:none"></span>');

      // Pfeil-Klick: auf-/zuklappen
      if (hasKids) {
        item.querySelector('.folder-toggle').addEventListener('click', e => {
          e.stopPropagation();
          toggleFolderCollapse(f.imap_path);
        });
      }

      // Item-Klick: Ordner öffnen (oder bei virtual: zuklappen)
      if (isVirtual && hasKids) {
        item.addEventListener('click', () => toggleFolderCollapse(f.imap_path));
      } else if (!isVirtual) {
        item.addEventListener('click', () => {
          state.activeAccount = account.id;
          state.activeFolder  = emailFolder;
          // Suche beenden wenn ein Ordner geklickt wird
          if (state.searchQuery) {
            state.searchQuery = '';
            const inp = document.getElementById('search-input');
            const clr = document.getElementById('search-clear');
            if (inp) inp.value = '';
            if (clr) clr.style.display = 'none';
          }
          document.querySelectorAll('.folder-item').forEach(el => el.classList.remove('active'));
          item.classList.add('active');
          renderReadFilterButtons();
          loadEmails(true);
        });

        // Drop-Ziel für Drag & Drop
        item.addEventListener('dragover', (e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          item.classList.add('drag-over');
        });
        item.addEventListener('dragleave', (e) => {
          // Nur entfernen wenn wir den Item wirklich verlassen (nicht bei Kind-Elementen)
          if (!item.contains(e.relatedTarget)) {
            item.classList.remove('drag-over');
          }
        });
        item.addEventListener('drop', async (e) => {
          e.preventDefault();
          item.classList.remove('drag-over');
          let dragIds;
          try { dragIds = JSON.parse(e.dataTransfer.getData('text/plain')); } catch { return; }
          if (!Array.isArray(dragIds) || dragIds.length === 0) return;
          moveEmailsToFolder(dragIds, f.imap_path);
        });
      }

      // Initiale Sichtbarkeit basierend auf Collapse-Zustand der Eltern
      if (delimiter) {
        for (const collapsed of state.collapsedFolders) {
          if (isDescendantOf(f.imap_path, collapsed, delimiter)) {
            item.style.display = 'none';
            break;
          }
        }
      }

      section.appendChild(item);
    });
    sidebar.appendChild(section);
  });
}

function renderEmails(reset) {
  const listEl = document.getElementById('email-list');
  if (reset) listEl.innerHTML = '';
  if (state.emails.length === 0) {
    listEl.innerHTML = '<div class="loading">Keine E-Mails.</div>';
    return;
  }
  appendEmails(state.emails, 0);
  // Nach dem Rendern prüfen ob der Filter alle E-Mails ausgefiltert hat
  if (listEl.querySelectorAll('.email-item').length === 0) {
    listEl.innerHTML = '<div class="loading">Keine E-Mails in dieser Kategorie.</div>';
  }
}

function buildEmailItem(email) {
  const tid = getThreadId(email);
  const isMultiThread = state.threadCount[tid] > 1;
  const isFirstOccurrence = !state.threadFirstSeen[tid];
  const isReply = isMultiThread && !isFirstOccurrence;

  state.threadFirstSeen[tid] = true;

  const item = document.createElement('div');
  item.className = 'email-item' +
    (!email.is_read ? ' unread' : '') +
    (email.id === state.activeEmailId ? ' active' : '') +
    (isMultiThread ? ' thread-member' : '') +
    (isMultiThread && isFirstOccurrence ? ' thread-first' : '') +
    (isReply ? ' reply' : '');
  item.dataset.id = email.id;
  item.dataset.thread = tid;

  const date = email.date_sent ? formatDate(email.date_sent) : '';
  // Im Sent-Ordner (auch in Suchergebnissen) Empfänger statt Absender zeigen.
  const showRecipient = isFlatFolder() || email.folder === 'Sent';
  const replyIcon = isReply
    ? '<span class="reply-icon">↳</span>'
    : (showRecipient ? '<span class="reply-icon sent-icon">→</span>' : '');
  const indent = isReply ? 'padding-left: 10px;' : '';
  const displayFrom = showRecipient
    ? ((email.to_emails && email.to_emails.length)
        ? email.to_emails.join(', ')
        : '–')
    : (email.reply_to || email.from_name || email.from_email || '–');
  const folderBadge = state.searchQuery
    ? `<span class="folder-badge">${escHtml(email.folder || '')}</span>` : '';

  const catLabel = state.categories.find(c => c.slug === email.ai_category)?.name;
  const aiBadge = (state.kiModeActive && email.ai_category && catLabel)
    ? `<span class="ai-category-badge ${escHtml(email.ai_category)}">${escHtml(catLabel)}</span>`
    : '';

  const spamBar = email.spam_suggested
    ? `<div class="spam-suggestion-bar">
         <span class="ssb-text">⚠ Möglicher Spam${email.spam_score ? ` (Score ${Number(email.spam_score).toFixed(2)})` : ''}</span>
         <button class="ssb-btn ssb-confirm" title="Ja, ist Spam — in Spam-Ordner verschieben">Spam</button>
         <button class="ssb-btn ssb-confirm-block" title="Spam und Absender blockieren">+ Absender blocken</button>
         <button class="ssb-btn ssb-dismiss" title="Doch kein Spam — Markierung entfernen">Behalten</button>
       </div>`
    : '';

  const attachmentClip = email.has_attachments
    ? '<span class="attachment-clip" title="Anhang"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 17.99 8.83l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></span>'
    : '';

  const inSpam = email.folder === 'Spam';
  const spamQuickActions = inSpam ? '' : `
      <button class="email-qa-btn qa-spam-vec" title="Spam – nur Vektor-Ähnlichkeit lernen">V</button>
      <button class="email-qa-btn qa-spam-block" title="Spam – Vektor lernen + Absender blocken">B</button>`;
  item.innerHTML = `
    <div class="email-flags">
      <span class="flag-answered${email.is_answered ? ' active' : ''}" title="Beantwortet">↩</span>
      <button class="flag-read-toggle${email.is_read ? '' : ' unread'}" title="${email.is_read ? 'Als ungelesen markieren' : 'Als gelesen markieren'}">●</button>
    </div>
    <div class="email-content">
      ${spamBar}
      <span class="email-from" style="${indent}">${replyIcon}${escHtml(displayFrom)}</span>
      <span class="email-date">${date}</span>
      <span class="email-subject" style="${indent}">${attachmentClip}<span class="email-subject-text">${escHtml(email.subject || '(kein Betreff)')}</span>${folderBadge}${aiBadge}</span>
    </div>
    <div class="email-quick-actions">
      <button class="email-qa-btn qa-delete" title="Löschen">×</button>${spamQuickActions}
    </div>
  `;

  item.querySelector('.qa-delete').addEventListener('click', e => {
    e.stopPropagation();
    deleteEmail(email, item);
  });
  const qaSpamVec = item.querySelector('.qa-spam-vec');
  if (qaSpamVec) {
    qaSpamVec.addEventListener('click', e => {
      e.stopPropagation();
      spamEmail(email, item);
    });
  }
  const qaSpamBlock = item.querySelector('.qa-spam-block');
  if (qaSpamBlock) {
    qaSpamBlock.addEventListener('click', e => {
      e.stopPropagation();
      spamEmail(email, item, { blockSender: true });
    });
  }

  const ssbConfirm = item.querySelector('.ssb-confirm');
  if (ssbConfirm) {
    ssbConfirm.addEventListener('click', e => {
      e.stopPropagation();
      spamEmail(email, item);
    });
  }
  const ssbConfirmBlock = item.querySelector('.ssb-confirm-block');
  if (ssbConfirmBlock) {
    ssbConfirmBlock.addEventListener('click', e => {
      e.stopPropagation();
      spamEmail(email, item, { blockSender: true });
    });
  }
  const ssbDismiss = item.querySelector('.ssb-dismiss');
  if (ssbDismiss) {
    ssbDismiss.addEventListener('click', e => {
      e.stopPropagation();
      email.spam_suggested = false;
      email.spam_score = null;
      const bar = item.querySelector('.spam-suggestion-bar');
      if (bar) bar.remove();
      api.spamSuggestionDismiss(email.id).catch(_ => {});
      _saveToCache();
    });
  }
  item.querySelector('.flag-read-toggle').addEventListener('click', async e => {
    e.stopPropagation();
    const nowRead = !email.is_read;
    email.is_read = nowRead;
    item.classList.toggle('unread', !nowRead);
    const btn = e.currentTarget;
    btn.classList.toggle('unread', !nowRead);
    btn.title = nowRead ? 'Als ungelesen markieren' : 'Als gelesen markieren';
    _adjustFolderCount(email.account, email.folder, nowRead ? -1 : +1);
    try {
      nowRead ? await api.markRead(email.id) : await api.markUnread(email.id);
    } catch (_) {
      // Rollback bei Fehler
      email.is_read = !nowRead;
      item.classList.toggle('unread', nowRead);
      btn.classList.toggle('unread', nowRead);
      btn.title = !nowRead ? 'Als ungelesen markieren' : 'Als gelesen markieren';
      _adjustFolderCount(email.account, email.folder, nowRead ? +1 : -1);
    }
  });

  item.addEventListener('click', (e) => {
    if (e.shiftKey) {
      rangeSelectEmail(item);
    } else if (e.metaKey || e.ctrlKey) {
      toggleSelectEmail(email, item);
    } else {
      clearSelection();
      selectEmail(email, item);
      openEmail(email, item);
    }
  });

  // Drag & Drop
  item.setAttribute('draggable', 'true');
  item.addEventListener('dragstart', (e) => {
    const dragIds = state.selectedEmails.has(email.id)
      ? [...state.selectedEmails]
      : [email.id];
    e.dataTransfer.setData('text/plain', JSON.stringify(dragIds));
    e.dataTransfer.effectAllowed = 'move';
    // Alle gezogenen Items visuell markieren
    const allItems = document.querySelectorAll('#email-list .email-item');
    allItems.forEach(el => {
      if (dragIds.includes(el.dataset.id)) el.classList.add('dragging');
    });
  });
  item.addEventListener('dragend', () => {
    document.querySelectorAll('.email-item.dragging').forEach(el => el.classList.remove('dragging'));
  });

  return item;
}

function appendEmails(emails, startIndex) {
  const listEl = document.getElementById('email-list');
  const placeholder = listEl.querySelector('.loading');
  if (placeholder) placeholder.remove();

  const newItems = [];

  emails.forEach((email) => {
    // Clientseitiger readFilter
    if (state.readFilter === 'unread' && email.is_read) return;
    if (state.readFilter === 'read'   && !email.is_read) return;
    // Clientseitiger KI-Kategorie-Filter: nur ungelesene mit passender Kategorie (gelesen = erledigt)
    if (state.kiCategoryFilter && (email.ai_category !== state.kiCategoryFilter || email.is_read)) return;
    const tid = getThreadId(email);
    const item = buildEmailItem(email);
    listEl.appendChild(item);
    newItems.push({ item, tid });
  });

  // Vorheriges thread-last neu bewerten falls neues Batch daran anschließt
  if (newItems.length > 0) {
    const prevItem = newItems[0].item.previousElementSibling;
    if (prevItem && prevItem.dataset.thread === newItems[0].tid) {
      prevItem.classList.remove('thread-last');
    }
  }

  // Isolierte thread-members bereinigen und thread-first/thread-last setzen
  cleanupThreadStyling();
}

function cleanupThreadStyling() {
  const listEl = document.getElementById('email-list');
  const items = Array.from(listEl.querySelectorAll('.email-item'));

  items.forEach((item, i) => {
    const tid = item.dataset.thread;
    const prevTid = i > 0 ? items[i - 1].dataset.thread : null;
    const nextTid = i < items.length - 1 ? items[i + 1].dataset.thread : null;

    const hasNeighbor = prevTid === tid || nextTid === tid;

    if (!hasNeighbor) {
      // Isoliert — kein Thread-Styling
      item.classList.remove('thread-member', 'thread-first', 'thread-last');
    } else {
      item.classList.add('thread-member');
      // thread-first: kein Vorgänger mit gleicher tid
      item.classList.toggle('thread-first', prevTid !== tid);
      // thread-last: kein Nachfolger mit gleicher tid
      item.classList.toggle('thread-last', nextTid !== tid);
    }
  });
}

// ── Multi-Select ────────────────────────────────────────────
function clearSelection() {
  state.selectedEmails.clear();
  document.querySelectorAll('.email-item.selected').forEach(el => el.classList.remove('selected'));
}

function selectEmail(email, item) {
  state.selectedEmails.add(email.id);
  item.classList.add('selected');
  state.lastClickedEl = item;
}

function toggleSelectEmail(email, item) {
  if (state.selectedEmails.has(email.id)) {
    state.selectedEmails.delete(email.id);
    item.classList.remove('selected');
  } else {
    state.selectedEmails.add(email.id);
    item.classList.add('selected');
  }
  state.lastClickedEl = item;
}

function rangeSelectEmail(item) {
  const allItems = Array.from(document.querySelectorAll('#email-list .email-item'));
  const currentIdx = allItems.indexOf(item);
  const anchorIdx = state.lastClickedEl ? allItems.indexOf(state.lastClickedEl) : currentIdx;
  const from = Math.min(currentIdx, anchorIdx);
  const to   = Math.max(currentIdx, anchorIdx);
  for (let i = from; i <= to; i++) {
    allItems[i].classList.add('selected');
    state.selectedEmails.add(allItems[i].dataset.id);
  }
  // Anker bleibt unverändert (weitere Shift+Clicks erweitern von gleichem Ankerpunkt)
}

// ── E-Mails verschieben ──────────────────────────────────────
const _TRASH_FOLDER_NAMES = new Set(['trash', 'papierkorb', 'deleted', 'deleted items', 'deleted messages']);

function moveEmailsToFolder(emailIds, targetImapPath) {
  const isTrash = _TRASH_FOLDER_NAMES.has((targetImapPath || '').toLowerCase());

  // Snapshot für Rollback
  const snapshot = emailIds.map(id => state.emails.find(e => e.id === id)).filter(Boolean);

  // Sofort aus DOM und State entfernen (Optimistic UI)
  emailIds.forEach(id => {
    const em = state.emails.find(e => e.id === id);
    if (em && !em.is_read) {
      em.is_read = true;
      _adjustFolderCount(em.account, em.folder, -1);
    }
    document.querySelector(`.email-item[data-id="${id}"]`)?.remove();
    state.emails = state.emails.filter(e => e.id !== id);
  });
  clearSelection();
  state.lastClickedEl = null;
  cleanupThreadStyling();
  if (emailIds.includes(state.activeEmailId)) {
    state.activeEmailId = null;
    showEmpty();
  }
  _saveToCache();
  _invalidateFolderCache(state.activeAccount, targetImapPath);

  // API-Calls parallel im Hintergrund — kein await, Funktion kehrt sofort zurück
  Promise.allSettled(emailIds.map(id => api.moveEmail(id, targetImapPath)))
    .then(results => {
      const failed = emailIds.filter((_, i) => results[i].status === 'rejected');
      if (failed.length > 0) {
        const failedEmails = snapshot.filter(e => failed.includes(e.id));
        failedEmails.forEach(e => {
          if (!e.is_read) _adjustFolderCount(e.account, e.folder, +1);
          e.is_read = false;
        });
        state.emails = [...failedEmails, ...state.emails];
        renderEmails(true);
        _saveToCache();
        alert(`Fehler beim Verschieben von ${failed.length} E-Mail(s).`);
      }
    });
}

// openEmail() liegt in js/email_detail.js (C4 Phase 2).
// spamEmail() liegt in js/spam.js (C4 Phase 2).

function deleteEmail(email, itemEl) {
  const next = itemEl.nextElementSibling || itemEl.previousElementSibling;
  const wasRead = email.is_read;

  // Sofort aus DOM und State entfernen (Optimistic UI)
  if (!email.is_read) {
    email.is_read = true;
    itemEl?.classList.remove('unread');
    _adjustFolderCount(email.account, email.folder, -1);
  }
  itemEl.remove();
  state.emails = state.emails.filter(em => em.id !== email.id);
  cleanupThreadStyling();
  if (next && next.dataset.id) {
    const nextEmail = state.emails.find(em => em.id === next.dataset.id);
    if (nextEmail) openEmail(nextEmail, next);
  } else {
    showEmpty();
  }

  // API im Hintergrund — kein await
  _saveToCache();
  api.deleteEmail(email.id).catch(e => {
    email.is_read = wasRead;
    state.emails = [email, ...state.emails];
    renderEmails(true);
    if (!wasRead) _adjustFolderCount(email.account, email.folder, +1);
    _saveToCache();
    alert('Löschen fehlgeschlagen: ' + e.message);
  });
}

// updateReadToggle(), linkify(), showEmpty() liegen in js/email_detail.js (C4 Phase 2).

function updateFooter() {
  const footer = document.getElementById('list-status');
  if (state.loadingMore) {
    const pct = state.totalItems > 0
      ? Math.round(state.emails.length / Math.min(state.totalItems, MAX_AUTO_LOAD) * 100)
      : 0;
    footer.innerHTML = `
      <div class="load-progress">
        <div class="load-bar" style="width:${pct}%"></div>
      </div>
      <span>Lade… ${state.emails.length} von ${state.totalItems}</span>
    `;
  } else {
    const visible = state.emails.filter(e => {
      if (state.readFilter === 'unread' && e.is_read) return false;
      if (state.readFilter === 'read'   && !e.is_read) return false;
      if (state.kiCategoryFilter && (e.ai_category !== state.kiCategoryFilter || e.is_read)) return false;
      return true;
    }).length;
    if (state.kiCategoryFilter || state.readFilter !== 'all') {
      footer.innerHTML = `${visible} gefilterte von ${state.totalItems} E-Mails`;
    } else {
      footer.innerHTML = `${visible} von ${state.totalItems} E-Mails`;
    }
  }
}

function updateListHeader() {
  const account = state.accounts.find(a => a.id === state.activeAccount);
  const name = account ? (account.name || account.from_email) : '';
  const folder = FOLDER_DISPLAY_NAMES[state.activeFolder] || state.activeFolder;
  document.getElementById('list-header-title').textContent =
    name ? `${folder} — ${name}` : folder;
  const viewToggle = document.querySelector('.view-toggle');
  if (viewToggle) viewToggle.style.display = isFlatFolder() ? 'none' : '';
}

function formatDate(iso) {
  const d = new Date(iso);
  const now = new Date();
  const days = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];
  const weekday = days[d.getDay()];
  const time = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) {
    return time;
  }
  return weekday + ' ' + d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: '2-digit' }) + ' ' + time;
}

async function _runKiSuggest(triggerBtn, contextElements) {
  const opts = state._currentReplyOpts;
  if (!opts || !state.activeEmailId) return;

  const origText = triggerBtn.textContent;
  triggerBtn.disabled = true;
  triggerBtn.classList.add('btn-loading');
  triggerBtn.textContent = 'Öffne Antwort…';
  closeKiAnalyzeSidebar();

  try {
    const opened = await openCompose(opts);
    if (opened === false) return;

    const bodyEl = document.getElementById('ci-body');
    const statusEl = document.getElementById('draft-status');
    bodyEl.contentEditable = 'false';
    bodyEl.innerHTML = '<span style="color:var(--text2);font-style:italic">KI generiert Antwort…</span>';
    statusEl.textContent = 'KI generiert Antwort…';
    statusEl.style.color = 'var(--text2)';

    const result = await api.ai.suggest(state.activeEmailId, 'neutral', contextElements.length ? contextElements : null);
    if (!result.text) throw new Error('KI hat keinen Text generiert.');

    const account = state.accounts.find(a => a.id === state.activeAccount);
    const sig = account && account.signature ? account.signature.trim() : '';
    const aiHtml = escHtml(result.text).replace(/\n/g, '<br>');
    const sigHtml = sig ? '<br><br>' + escHtml(sig).replace(/\n/g, '<br>') : '';
    bodyEl.innerHTML = aiHtml + sigHtml;

    try {
      const range = document.createRange();
      range.setStart(bodyEl, 0);
      range.collapse(true);
      const sel = window.getSelection();
      if (sel) { sel.removeAllRanges(); sel.addRange(range); }
    } catch (_) {}
    requestAnimationFrame(() => { document.getElementById('compose-mode').scrollTop = 0; });

    scheduleDraftSave();
    statusEl.textContent = '';
    statusEl.style.color = '';

  } catch (e) {
    const msg = e.message || '';
    if (msg.includes('529') || msg.includes('overloaded') || msg.includes('überlastet')) {
      alert('Die KI ist gerade überlastet. Bitte in einem Moment erneut versuchen.');
    } else {
      alert('KI-Antwort fehlgeschlagen: ' + msg);
    }
  } finally {
    triggerBtn.disabled = false;
    triggerBtn.classList.remove('btn-loading');
    triggerBtn.textContent = origText;
    document.getElementById('ci-body').contentEditable = 'true';
  }
}

function closeKiAnalyzeSidebar() {
  document.getElementById('ki-analyze-sidebar').classList.remove('open');
}

async function openKiAnalyzeSidebar(emailId, fromEmail) {

  const sidebar = document.getElementById('ki-analyze-sidebar');
  const body = document.getElementById('ki-analyze-body');
  sidebar.classList.add('open');
  body.innerHTML = '<div class="loading">KI analysiert…</div>';

  const [analyzeResult, xanoResult] = await Promise.allSettled([
    api.ai.analyze(emailId),
    fromEmail ? api.xano.userInfo(fromEmail) : Promise.resolve(null),
  ]);

  // Xano-Info-Karte rendern
  let xanoHtml = '';
  if (xanoResult.status === 'fulfilled' && xanoResult.value) {
    const ud = xanoResult.value.userdata;
    if (Array.isArray(ud) && ud.length) {
      const u = ud[0];
      const rollenHtml = (u.rollen || []).map(r =>
        `<span class="xano-role">${escHtml(r)}</span>`
      ).join('');
      const mahnHtml = u.mahnstatus && u.mahnstatus !== '0'
        ? `<span class="xano-mahnstatus">Mahnstufe ${escHtml(u.mahnstatus)}</span>` : '';
      const lastLoginStr = u.lastlogin
        ? new Date(u.lastlogin).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })
        : 'nie';
      xanoHtml = `<div class="xano-card">
        <div class="xano-row"><span class="xano-lbl">FM-ID</span><span>${escHtml(u.fm_id || '–')}</span></div>
        ${u.rollen?.length ? `<div class="xano-row"><span class="xano-lbl">Rollen</span><span class="xano-roles">${rollenHtml}</span></div>` : ''}
        <div class="xano-row"><span class="xano-lbl">Login</span><span>${escHtml(lastLoginStr)}</span></div>
        ${mahnHtml ? `<div class="xano-row">${mahnHtml}</div>` : ''}
      </div>`;
    } else if (typeof ud === 'string') {
      xanoHtml = `<div class="xano-card xano-card--none">Kein HPA24-Account</div>`;
    }
  }

  // KI-Analyse-Items rendern
  let itemsHtml = '';
  let items = [];
  if (analyzeResult.status === 'fulfilled') {
    items = analyzeResult.value.items || [];
    if (items.length) {
      itemsHtml = items.map((item, i) =>
        `<div class="ki-analyze-item" data-index="${i}" data-original-draft="${escHtml(item.draft || '')}">
          <div class="ki-analyze-top">
            <div class="ki-analyze-element">${escHtml(item.element)}</div>
            <div class="ki-analyze-action">${escHtml(item.action)}</div>
          </div>
          <textarea class="ki-analyze-draft" rows="3">${escHtml(item.draft || '')}</textarea>
          <div class="ki-analyze-save-row">
            <button class="ki-analyze-save-btn">Speichern</button>
            <span class="ki-analyze-save-status"></span>
          </div>
        </div>`
      ).join('');
    } else {
      itemsHtml = '<div class="ki-analyze-empty">Keine Elemente erkannt.</div>';
    }
  } else {
    itemsHtml = `<div class="ki-analyze-error">Fehler: ${escHtml(analyzeResult.reason?.message || '?')}</div>`;
  }

  body.innerHTML = xanoHtml + itemsHtml;

  // Click auf oberen Teil → Auswahl togglen
  body.querySelectorAll('.ki-analyze-top').forEach(top => {
    top.addEventListener('click', () => top.closest('.ki-analyze-item').classList.toggle('selected'));
  });

  // Speichern-Buttons
  body.querySelectorAll('.ki-analyze-item').forEach(card => {
    const btn = card.querySelector('.ki-analyze-save-btn');
    const status = card.querySelector('.ki-analyze-save-status');
    const textarea = card.querySelector('.ki-analyze-draft');
    const idx = parseInt(card.dataset.index, 10);
    const item = items[idx] || {};

    btn.addEventListener('click', async () => {
      btn.disabled = true;
      status.textContent = '…';
      status.className = 'ki-analyze-save-status';
      const currentDraft = textarea.value.trim();
      const wasEdited = currentDraft !== (item.draft || '').trim();
      try {
        await api.responsePatterns.save({
          account_id:   state.activeAccount || '',
          element_text: item.element || '',
          action:       item.action || '',
          draft_text:   currentDraft,
          was_edited:   wasEdited,
        });
        status.textContent = '✓ Gespeichert';
        status.classList.add('ki-save-ok');
      } catch (e) {
        status.textContent = 'Fehler';
        status.classList.add('ki-save-err');
      } finally {
        btn.disabled = false;
      }
    });
  });
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function isFlatFolder() {
  // Im Gesendet-Ordner nutzlos zu nach Konversation/Absender zu gruppieren
  // (Absender ist immer Stefan). Daher flache, chronologische Liste.
  return state.activeFolder === 'Sent';
}

function getThreadId(email) {
  if (isFlatFolder()) return email.id;
  return email.display_thread_id || email.thread_id || email.message_id || email.id;
}

async function fetchFlatEmails(params) {
  const data = await api.getEmails(params);
  const total = data.totalItems || 0;
  const perPage = data.perPage || params.limit || 50;
  const page = data.page || params.page || 1;
  return {
    items: data.items || [],
    totalItems: total,
    hasMore: page * perPage < total,
  };
}

// setupComposeToolbar(), confirmDiscard(), composeHasContent() liegen in js/compose.js (C4 Phase 2 / A).

// ── Anhang-Download-Anzeige ──────────────────────────────────
function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadAttachments(email) {
  const el = document.getElementById('detail-attachments');
  el.style.display = 'none';
  el.innerHTML = '';
  if (!email.has_attachments) return;
  try {
    const data = await api.getAttachments(email.id);
    const items = data.items || [];
    if (!items.length) return;
    // Chips ohne href bauen, URL on-click frisch signieren (Token-TTL nur 5min — nicht beim Listen-Render verbrennen)
    items.forEach(att => {
      const chip = document.createElement('a');
      chip.className = 'attachment-chip';
      chip.href = '#';
      chip.target = '_blank';
      chip.rel = 'noopener noreferrer';
      chip.innerHTML = `
        <span class="attachment-icon">📎</span>
        <span class="attachment-name" title="${escHtml(att.filename)}">${escHtml(att.filename)}</span>
        <span class="attachment-size">${formatBytes(att.size_bytes || 0)}</span>
      `;
      chip.addEventListener('click', async (ev) => {
        ev.preventDefault();
        try {
          const url = await api.attachmentDownloadUrl(att.id);
          window.open(url, '_blank', 'noopener,noreferrer');
        } catch (e) {
          console.warn('attachment sign failed:', e);
        }
      });
      el.appendChild(chip);
    });
    el.style.display = 'flex';
  } catch (e) {
    console.warn('loadAttachments:', e);
  }
}
// ─────────────────────────────────────────────────────────────

// Inline Compose state (_draftId, _draftTimer, _editingDraftItemEl, _composeAttachments, _replyToEmailId) liegt in js/compose.js (C4 Phase 2 / A).

function showTab(tab) {
  const isCompose = tab === 'compose';
  document.getElementById('view-mode').style.display   = isCompose ? 'none' : 'flex';
  document.getElementById('compose-mode').style.display = isCompose ? 'flex' : 'none';
  document.getElementById('tab-email').classList.toggle('active', !isCompose);
  document.getElementById('tab-compose').classList.toggle('active', isCompose);
}

// openCompose() liegt in js/compose.js (C4 Phase 2 / A).

// closeCompose(), scheduleDraftSave(), saveDraft(), btn-compose-cancel + btn-send-inline Listener liegen in js/compose.js (C4 Phase 2 / A).
// ────────────────────────────────────────────────────────────

// Massenversand + Test-Send liegen in js/compose.js (C4 Phase 2 / B).

document.getElementById('btn-new-email').addEventListener('click', () => {
  openCompose({});
});

document.getElementById('btn-sync').addEventListener('click', async () => {
  _invalidateFolderCache(state.activeAccount, state.activeFolder);
  await api.syncRun();
  await loadEmails(true);
});

// ── Zoom ──────────────────────────────────────────────────────
document.getElementById('btn-zoom').addEventListener('click', (e) => {
  const action = e.target.closest('[data-zoom-action]')?.dataset.zoomAction;
  const idx = ZOOM_LEVELS.indexOf(_iframeZoom);
  const safeIdx = idx >= 0 ? idx : ZOOM_LEVELS.indexOf(DEFAULT_ZOOM);

  if (action === 'in') {
    _iframeZoom = ZOOM_LEVELS[Math.min(ZOOM_LEVELS.length - 1, safeIdx + 1)];
  } else if (action === 'out') {
    _iframeZoom = ZOOM_LEVELS[Math.max(0, safeIdx - 1)];
  } else {
    _iframeZoom = 1.0;
  }

  _applyZoom();
});

// ── KI-Event-Listener ────────────────────────────────────────
document.getElementById('btn-ki-mode').addEventListener('click', toggleKiMode);

document.getElementById('btn-ki-triage').addEventListener('click', runKiTriage);

document.getElementById('ki-toolbar').addEventListener('click', (e) => {
  const btn = e.target.closest('.ki-filter-btn');
  if (!btn) return;
  document.querySelectorAll('.ki-filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.kiCategoryFilter = btn.dataset.filter || '';
  loadEmails(true);
});

// ── KI-Kategorie-Bar im Detail-Panel ────────────────────────
function _updateDetailKiBar(currentCat) {
  document.querySelectorAll('#detail-ki-bar .ki-cat-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.cat === currentCat);
  });
}

document.getElementById('detail-ki-bar').addEventListener('click', async (e) => {
  const btn = e.target.closest('.ki-cat-btn');
  if (!btn || !state.activeEmailId) return;

  const newCat = btn.dataset.cat;
  const email = state.emails.find(em => em.id === state.activeEmailId);
  if (!email) return;

  try {
    await api.setCategory(state.activeEmailId, newCat);
  } catch (err) {
    alert('Kategorie konnte nicht gespeichert werden: ' + err.message);
    return;
  }
  if (newCat) {
    api.saveTriageExample(state.activeEmailId, newCat).catch(() => {});
  }

  // Lokalen State aktualisieren
  email.ai_category = newCat;
  _updateDetailKiBar(newCat);

  // Badge in der Liste aktualisieren
  const itemEl = document.querySelector(`.email-item[data-id="${state.activeEmailId}"]`);
  if (itemEl) {
    const badge = itemEl.querySelector('.ai-category-badge');
    if (badge) badge.textContent = state.categories.find(c => c.slug === newCat)?.name || newCat;
  }

  // E-Mail aus gefilterter Liste entfernen, wenn neue Kategorie nicht zum Filter passt
  if (state.kiCategoryFilter && state.kiCategoryFilter !== newCat) {
    state.emails = state.emails.filter(em => em.id !== state.activeEmailId);
    if (itemEl) itemEl.remove();
    state.activeEmailId = null;
    showEmpty();
    updateFooter();
  }
});
// ─────────────────────────────────────────────────────────────

document.getElementById('ki-analyze-close').addEventListener('click', closeKiAnalyzeSidebar);
document.getElementById('ki-analyze-cancel').addEventListener('click', closeKiAnalyzeSidebar);
document.getElementById('ki-analyze-ok').addEventListener('click', () => {
  const selected = [...document.querySelectorAll('.ki-analyze-item.selected')];
  const actions = selected.map(el => {
    const ta = el.querySelector('.ki-analyze-draft');
    return ta ? ta.value.trim() : (el.querySelector('.ki-analyze-action')?.textContent || '');
  }).filter(Boolean);
  _runKiSuggest(document.getElementById('ki-analyze-ok'), actions);
});

// Kontext-Menü
const ctxMenu = document.getElementById('ctx-menu');

document.addEventListener('click', () => ctxMenu.style.display = 'none');
document.addEventListener('contextmenu', e => {
  const item = e.target.closest('.email-item');
  if (!item) return;
  e.preventDefault();

  const posLeft = `${Math.min(e.clientX, window.innerWidth - 220)}px`;
  const posTop  = `${Math.min(e.clientY, window.innerHeight - 120)}px`;

  // ── Bulk-Aktionen bei Mehrfachauswahl ──────────────────────
  if (state.selectedEmails.size > 1) {
    const count = state.selectedEmails.size;
    ctxMenu.innerHTML = `
      <div class="ctx-item" data-action="bulk-read">✓ ${count} E-Mails als gelesen markieren</div>
      <div class="ctx-item" data-action="bulk-unread">◯ ${count} E-Mails als ungelesen markieren</div>
      <div class="ctx-item ctx-danger" data-action="bulk-delete">🗑 ${count} E-Mails in Papierkorb</div>
    `;
    ctxMenu.style.display = 'block';
    ctxMenu.style.left = posLeft;
    ctxMenu.style.top  = posTop;

    const bulkSetRead = async (newState) => {
      ctxMenu.style.display = 'none';
      const ids = [...state.selectedEmails];
      clearSelection();

      // Sofort State und UI aktualisieren
      const emailRefs = ids.map(id => {
        const em = state.emails.find(e => e.id === id);
        if (em) em.is_read = newState;
        if (state.activeEmailId === id && em) {
          const el = document.querySelector(`.email-item[data-id="${id}"]`);
          if (el) updateReadToggle(em, el);
        }
        return { id, account: em?.account ?? '', folder: em?.folder ?? '', imap_uid: em?.imap_uid ?? null };
      });
      renderEmails(true);
      updateFooter();
      loadUnreadCounts();

      // API im Hintergrund
      try {
        await api.bulkMarkRead(emailRefs, newState);
      } catch (e) {
        console.error('Bulk-Markierung fehlgeschlagen:', e);
      }
    };

    const bulkDelete = async () => {
      ctxMenu.style.display = 'none';
      const ids = [...state.selectedEmails];
      clearSelection();

      // Sofort aus State und DOM entfernen
      ids.forEach(id => { state.emails = state.emails.filter(em => em.id !== id); });
      renderEmails(true);
      cleanupThreadStyling();
      updateFooter();
      loadUnreadCounts();
      if (!document.querySelector('.email-item.active')) showEmpty();

      // API im Hintergrund
      await Promise.allSettled(ids.map(id => api.deleteEmail(id)));
    };

    ctxMenu.querySelector('[data-action="bulk-read"]').onclick   = () => bulkSetRead(true);
    ctxMenu.querySelector('[data-action="bulk-unread"]').onclick = () => bulkSetRead(false);
    ctxMenu.querySelector('[data-action="bulk-delete"]').onclick = bulkDelete;
    return;
  }

  // ── Einzel-Aktion ───────────────────────────────────────────
  const emailId = item.dataset.id;
  const email = state.emails.find(em => em.id === emailId);
  if (!email) return;

  const spamItem = email.folder === 'Spam'
    ? ''
    : '<div class="ctx-item ctx-danger" data-action="spam">Als Spam markieren</div>';
  ctxMenu.innerHTML = `
    <div class="ctx-item ${email.is_read ? 'ctx-inactive' : ''}" data-action="mark-read">Als gelesen markieren</div>
    <div class="ctx-item ${!email.is_read ? 'ctx-inactive' : ''}" data-action="mark-unread">Als ungelesen markieren</div>
    <div class="ctx-item ctx-danger" data-action="delete">In Papierkorb</div>
    ${spamItem}
  `;
  ctxMenu.style.display = 'block';
  ctxMenu.style.left = posLeft;
  ctxMenu.style.top  = posTop;

  const setRead = async (newState) => {
    ctxMenu.style.display = 'none';
    email.is_read = newState;
    item.classList.toggle('unread', !newState);
    if (state.activeEmailId === email.id) updateReadToggle(email, item);
    _adjustFolderCount(email.account, email.folder, newState ? -1 : +1);
    try {
      await (newState ? api.markRead(email.id) : api.markUnread(email.id));
    } catch (_) {
      email.is_read = !newState;
      item.classList.toggle('unread', newState);
      if (state.activeEmailId === email.id) updateReadToggle(email, item);
      _adjustFolderCount(email.account, email.folder, newState ? +1 : -1);
    }
  };

  ctxMenu.querySelector('[data-action="mark-read"]').onclick   = () => setRead(true);
  ctxMenu.querySelector('[data-action="mark-unread"]').onclick = () => setRead(false);
  ctxMenu.querySelector('[data-action="delete"]').onclick = () => {
    ctxMenu.style.display = 'none';
    deleteEmail(email, item);
  };
  const ctxSpam = ctxMenu.querySelector('[data-action="spam"]');
  if (ctxSpam) {
    ctxSpam.onclick = () => {
      ctxMenu.style.display = 'none';
      spamEmail(email, item);
    };
  }
});

// ── Detail-Tabs ──────────────────────────────────────────────
document.getElementById('tab-email').addEventListener('click', () => showTab('email'));
document.getElementById('tab-compose').addEventListener('click', () => showTab('compose'));
// ─────────────────────────────────────────────────────────────

// Compose-Anhänge (_renderComposeAttachments, _uploadFiles, btn-attach, ci-file-input, Drag&Drop) und Address-Chip-Felder (makeAddressField, _toField, _ccField) liegen in js/compose.js (C4 Phase 2 / C).

// ── Account-Einstellungen Modal ──────────────────────────────
let _editingAccountId = null;

function openAccountSettings(account) {
  _editingAccountId = account.id;
  document.getElementById('account-modal-title').textContent =
    `Einstellungen: ${account.name || account.from_email}`;
  document.getElementById('am-name').value = account.name || '';
  document.getElementById('am-from-name').value = account.from_name || '';
  document.getElementById('am-signature').value = account.signature || '';
  const colorInput = document.getElementById('am-color');
  const colorHex   = document.getElementById('am-color-hex');
  const initialColor = account.color_tag || '#888888';
  colorInput.value = initialColor;
  colorHex.textContent = initialColor;
  colorInput.oninput = () => { colorHex.textContent = colorInput.value; };
  document.getElementById('account-modal-status').textContent = '';
  document.getElementById('account-modal-overlay').style.display = 'flex';
}

function closeAccountSettings() {
  document.getElementById('account-modal-overlay').style.display = 'none';
  _editingAccountId = null;
}

document.getElementById('account-modal-close').addEventListener('click', closeAccountSettings);
document.getElementById('account-modal-cancel').addEventListener('click', closeAccountSettings);
document.getElementById('account-modal-overlay').addEventListener('click', (e) => {
  if (e.target === document.getElementById('account-modal-overlay')) closeAccountSettings();
});

document.getElementById('account-modal-save').addEventListener('click', async () => {
  if (!_editingAccountId) return;
  const statusEl = document.getElementById('account-modal-status');
  const data = {
    name:       document.getElementById('am-name').value.trim(),
    from_name:  document.getElementById('am-from-name').value.trim(),
    signature:  document.getElementById('am-signature').value,
    color_tag:  document.getElementById('am-color').value,
  };
  statusEl.textContent = 'Speichert…';
  try {
    await api.updateAccount(_editingAccountId, data);
    // Lokalen State aktualisieren
    const acc = state.accounts.find(a => a.id === _editingAccountId);
    if (acc) Object.assign(acc, data);
    statusEl.textContent = 'Gespeichert ✓';
    setTimeout(closeAccountSettings, 800);
    renderSidebar(); // Sidebar neu rendern (falls Name geändert)
    loadUnreadCounts();
  } catch (e) {
    statusEl.textContent = 'Fehler: ' + e.message;
  }
});
// ─────────────────────────────────────────────────────────────

// Spam-Rules-Verwaltung liegt in js/spam.js (C4 Phase 2).

init();

