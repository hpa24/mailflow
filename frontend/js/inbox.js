const FIRST_PAGE_SIZE = 50;  // Erste Seite klein → sofort sichtbar
const PAGE_SIZE = 500;
const MAX_AUTO_LOAD = 1500; // Automatisch bis zu dieser Anzahl laden

// ── Zoom ──────────────────────────────────────────────────────
const ZOOM_LEVELS = [0.75, 1.0, 1.25, 1.5];
let _iframeZoom = 1.0;
let _activeIframe = null;
let _activeIframeBaseHtml = null; // srcdoc nach CID-Ersatz, ohne Zoom-CSS

function _withZoom(html) {
  const style = `<style>html,body{zoom:${_iframeZoom}}</style>`;
  return html.includes('</head>') ? html.replace('</head>', style + '</head>') : style + html;
}

function _applyZoom() {
  const btn = document.getElementById('btn-zoom');
  if (btn) btn.textContent = Math.round(_iframeZoom * 100) + '%';
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

function startEventSource() {
  let es = null;

  async function connect() {
    let url;
    try {
      url = await apiEventSourceUrl();
    } catch (_) {
      setTimeout(connect, 10_000);
      return;
    }
    es = new EventSource(url);

    es.onmessage = async (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'new-mail') {
          await silentRefresh();
        } else if (data.type === 'send-result') {
          _handleSendResult(data);
          if (data.success) scheduleSentTodayRefresh();
        }
      } catch (_) {}
    };

    es.onerror = () => {
      es.close();
      setTimeout(connect, 10_000);
    };
  }

  connect();
  window.addEventListener('beforeunload', () => { if (es) es.close(); }, { once: true });
}

// ── Versand-Benachrichtigungen ───────────────────────────────

const _sendNotifContainer = document.getElementById('send-notifications');

function _addSendNotif(jobId, to, subject) {
  const shortTo      = to.length > 35 ? to.slice(0, 35) + '…' : to;
  const shortSubject = subject.length > 40 ? subject.slice(0, 40) + '…' : subject;

  const el = document.createElement('div');
  el.className = 'send-notif pending';
  el.dataset.jobId = jobId;
  el.innerHTML = `
    <span class="send-notif-icon">⏳</span>
    <span class="send-notif-text">Wird gesendet an <strong>${_escHtml(shortTo)}</strong> — ${_escHtml(shortSubject)}</span>
  `;
  _sendNotifContainer.appendChild(el);
}

function _handleSendResult(data) {
  // Bulk-Hook: wenn die job_id zu einem laufenden Massenversand gehört,
  // übernimmt das Status-Modal die Anzeige — keine normale Notif.
  if (_bulkTracking && _bulkTracking.byJobId.has(data.job_id)) {
    _bulkApplyResult(data);
    return;
  }
  const el = _sendNotifContainer.querySelector(`[data-job-id="${data.job_id}"]`);
  if (!el) return;

  const shortTo      = (data.to      || '').length > 35 ? data.to.slice(0, 35) + '…' : (data.to || '');
  const shortSubject = (data.subject || '').length > 40 ? data.subject.slice(0, 40) + '…' : (data.subject || '');

  if (data.success) {
    el.className = 'send-notif success';
    el.innerHTML = `
      <span class="send-notif-icon">✓</span>
      <span class="send-notif-text">Gesendet an <strong>${_escHtml(shortTo)}</strong> — ${_escHtml(shortSubject)}</span>
    `;
    setTimeout(() => el.remove(), 4000);
  } else {
    const errMsg = (data.error || 'Unbekannter Fehler').slice(0, 80);
    el.className = 'send-notif error';
    const dismiss = document.createElement('button');
    dismiss.className = 'send-notif-dismiss';
    dismiss.title = 'Schließen';
    dismiss.textContent = '×';
    dismiss.onclick = () => el.remove();
    el.innerHTML = `
      <span class="send-notif-icon">✗</span>
      <span class="send-notif-text">Fehler an <strong>${_escHtml(shortTo)}</strong> — ${_escHtml(shortSubject)}: ${_escHtml(errMsg)}</span>
    `;
    el.appendChild(dismiss);
  }
}

function _escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// URLs in Textknoten zu klickbaren <a>-Tags machen. Bestehende <a> bleiben unangetastet.
function _linkifyHtml(html) {
  if (!html) return html;
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      let p = node.parentNode;
      while (p && p !== tpl.content) {
        const name = p.nodeName;
        if (name === 'A' || name === 'SCRIPT' || name === 'STYLE') return NodeFilter.FILTER_REJECT;
        p = p.parentNode;
      }
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const URL_RE = /(https?:\/\/[^\s<>"']+|www\.[^\s<>"']+)/g;
  const targets = [];
  let n;
  while ((n = walker.nextNode())) {
    if (n.nodeValue && URL_RE.test(n.nodeValue)) targets.push(n);
    URL_RE.lastIndex = 0;
  }
  for (const textNode of targets) {
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let lastIdx = 0;
    URL_RE.lastIndex = 0;
    let m;
    while ((m = URL_RE.exec(text))) {
      let url = m[0];
      const trail = url.match(/[.,;:!?)\]}>]+$/);
      if (trail) url = url.slice(0, -trail[0].length);
      if (m.index > lastIdx) {
        frag.appendChild(document.createTextNode(text.slice(lastIdx, m.index)));
      }
      const a = document.createElement('a');
      a.href = url.startsWith('www.') ? 'https://' + url : url;
      a.textContent = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      frag.appendChild(a);
      lastIdx = m.index + url.length;
      URL_RE.lastIndex = lastIdx;
    }
    if (lastIdx < text.length) {
      frag.appendChild(document.createTextNode(text.slice(lastIdx)));
    }
    textNode.parentNode.replaceChild(frag, textNode);
  }
  return tpl.innerHTML;
}

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
  document.title = total > 0 ? `(${Math.min(total, 99)}) Mailflow` : 'Mailflow';
  if ('setAppBadge' in navigator) {
    total > 0 ? navigator.setAppBadge(total) : navigator.clearAppBadge();
  }
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

async function openEmail(email, itemEl) {
  state.activeEmailId = email.id;
  document.querySelectorAll('.email-item').forEach(el => el.classList.remove('active'));
  itemEl.classList.add('active');

  // Wenn Compose offen ist: zum E-Mail-Tab wechseln (Compose bleibt erhalten)
  if (document.getElementById('detail-tabs').style.display !== 'none') {
    showTab('email');
  }

  const header = document.getElementById('detail-header');
  const body = document.getElementById('detail-body');
  const empty = document.getElementById('detail-empty');
  const actions = document.getElementById('detail-actions');

  // Zoom-State für neue E-Mail zurücksetzen
  _activeIframe = null;
  _activeIframeBaseHtml = null;
  _iframeZoom = 1.0;
  body.style.zoom = '';
  const _zoomBtn = document.getElementById('btn-zoom');
  if (_zoomBtn) _zoomBtn.textContent = '100%';

  empty.style.display = 'none';
  header.style.display = 'block';
  actions.style.display = 'flex';
  body.style.display = 'block';
  document.getElementById('detail-attachments').style.display = 'none';

  // KI-Kategorie-Bar: nur im KI-Modus anzeigen
  const kiBar = document.getElementById('detail-ki-bar');
  kiBar.style.display = state.kiModeActive ? 'flex' : 'none';
  if (state.kiModeActive) {
    _updateDetailKiBar(email.ai_category || '');
  }

  document.getElementById('detail-subject').textContent = email.subject || '(kein Betreff)';
  const _renderDetailMeta = (e) => {
    const ccList = (e.cc_emails || []).filter(Boolean);
    document.getElementById('detail-meta').innerHTML = `
      <span style="width:60px;display:inline-block;">Von:</span> ${escHtml(e.from_name ? `${e.from_name} <${e.from_email}>` : e.from_email)}<br>
      <span style="width:60px;display:inline-block;">An:</span> ${escHtml((e.to_emails || []).join(', '))}<br>
      ${ccList.length ? `<span style="width:60px;display:inline-block;">Cc:</span> ${escHtml(ccList.join(', '))}<br>` : ''}
      <span style="width:60px;display:inline-block;">Datum:</span> ${e.date_sent ? new Date(e.date_sent).toLocaleString('de-DE') : '–'}
      ${e.is_answered ? '<br><span style="color:var(--accent);font-size:12px">↩ Beantwortet</span>' : ''}
    `;
  };
  _renderDetailMeta(email);
  body.textContent = 'Lade…';
  document.getElementById('btn-reply').onclick = null;
  document.getElementById('btn-forward').onclick = null;
  document.getElementById('btn-edit-draft').onclick = null;

  const isDraft = (email.folder || '').toLowerCase().includes('draft');

  // Buttons je nach Ordner ein-/ausblenden
  document.getElementById('btn-edit-draft').style.display  = isDraft ? '' : 'none';
  document.getElementById('btn-send-draft').style.display  = isDraft ? '' : 'none';
  document.getElementById('btn-sync-draft').style.display  = isDraft ? '' : 'none';
  document.getElementById('btn-reply').style.display       = isDraft ? 'none' : '';
  document.getElementById('btn-forward').style.display     = isDraft ? 'none' : '';
  document.getElementById('btn-toggle-read').style.display = isDraft ? 'none' : '';
  const _inSpam = email.folder === 'Spam';
  document.getElementById('btn-spam').style.display        = (isDraft || _inSpam) ? 'none' : '';
  document.getElementById('btn-spam-block').style.display  = (isDraft || _inSpam) ? 'none' : '';

  // KI-Suggest-Button: nur anzeigen wenn KI-Modus aktiv und kein Draft
  const kiSuggestBtn = document.getElementById('btn-ki-suggest');
  kiSuggestBtn.style.display = (state.kiModeActive && !isDraft) ? '' : 'none';
  kiSuggestBtn.onclick = null;

  // KI-Analyse-Button: nur anzeigen wenn KI-Modus aktiv und kein Draft
  const kiAnalyzeBtn = document.getElementById('btn-ki-analyze');
  kiAnalyzeBtn.style.display = (state.kiModeActive && !isDraft) ? '' : 'none';
  kiAnalyzeBtn.onclick = () => openKiAnalyzeSidebar(email.id, email.from_email);

  // Sidebar schließen wenn neue E-Mail geöffnet wird
  closeKiAnalyzeSidebar();

  // Handler wird erst nach Full-Email-Load gesetzt (braucht quote-Text aus full)

  try {
    const full = await api.getEmail(email.id);
    _renderDetailMeta(full);
    // body_plain bevorzugen; Fallback: plain text aus body_html extrahieren (HTML-only-E-Mails)
    let text = full.body_plain || '';
    if (!text && full.body_html) {
      const _tmp = document.createElement('div');
      _tmp.innerHTML = full.body_html
        .replace(/<br\s*\/?>/gi, '\n')
        .replace(/<\/p>/gi, '\n\n')
        .replace(/<\/div>/gi, '\n')
        .replace(/<\/tr>/gi, '\n')
        .replace(/<\/li>/gi, '\n');
      text = (_tmp.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
    }
    if (full.body_html && full.body_html.trim()) {
      // HTML in sandboxiertem Iframe rendern.
      // allow-same-origin OHNE allow-scripts: Parent kann contentDocument
      // lesen (Höhenmessung), aber eingebettetes E-Mail-JS kann nicht laufen.
      const iframe = document.createElement('iframe');
      iframe.setAttribute('sandbox', 'allow-popups allow-popups-to-escape-sandbox allow-same-origin');
      iframe.style.cssText = 'width:100%;border:none;display:block;';

      const injectCss = `<style>img{max-width:100%!important;height:auto!important}</style>`;
      const injectBase = `<base target="_blank">`;

      let htmlToRender;
      const isFullDoc = /<html[\s>]/i.test(full.body_html);
      if (isFullDoc) {
        // Vollständiges HTML-Dokument: eigenes <head> mit charset behalten,
        // nur <base> + CID-Fix injizieren. Kein erneutes Einwickeln (verhindert
        // doppelte <html>-Tags und Charset-Durcheinander).
        let h = full.body_html;
        // Evtl. vorhandene <base>-Tags ersetzen, damit target="_blank" greift
        h = h.replace(/<base\b[^>]*>/gi, '');
        if (/<head[\s>]/i.test(h)) {
          h = h.replace(/(<head[^>]*>)/i, `$1${injectBase}${injectCss}`);
        } else {
          h = injectBase + injectCss + h;
        }
        htmlToRender = h;
      } else {
        // HTML-Fragment: in minimales Dokument einwickeln
        htmlToRender = `<!DOCTYPE html><html><head><meta charset="utf-8">${injectBase}${injectCss}
          <style>
            body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
                 font-size:14px;padding:16px;margin:0;color:#1c1c1e;word-wrap:break-word;line-height:1.5}
            a{color:#0a84ff}
            pre,code{white-space:pre-wrap;background:#f5f5f7;padding:2px 4px;border-radius:3px}
            blockquote{border-left:3px solid #ccc;margin-left:0;padding-left:12px;color:#666}
          </style></head><body>${full.body_html}</body></html>`;
      }

      // Höhe vom Parent aus messen — kein Script-Inject in fremdes Mail-HTML.
      // Nutzt contentDocument (durch allow-same-origin erlaubt). Reagiert auf
      // Initial-Load + spätere Layout-Änderungen (z.B. nachladende Bilder).
      const setHeight = () => {
        try {
          const doc = iframe.contentDocument;
          if (!doc) return;
          const h = Math.max(
            doc.documentElement?.scrollHeight || 0,
            doc.body?.scrollHeight || 0
          );
          if (h > 0) iframe.style.height = (h + 32) + 'px';
        } catch (_) { /* cross-origin oder noch nicht geladen */ }
      };
      let resizeObs = null;
      iframe.addEventListener('load', () => {
        setHeight();
        try {
          const doc = iframe.contentDocument;
          if (doc && doc.body && 'ResizeObserver' in window) {
            resizeObs = new ResizeObserver(setHeight);
            resizeObs.observe(doc.body);
          }
          // Spät ladende Bilder triggern keinen ResizeObserver-Reflow zuverlässig
          if (doc) {
            doc.querySelectorAll('img').forEach(img => {
              if (!img.complete) img.addEventListener('load', setHeight, { once: true });
            });
          }
        } catch (_) { /* same-origin-Zugriff fehlgeschlagen */ }
      });
      // ResizeObserver + Image-Listener aufräumen, wenn iframe entfernt wird
      const cleanup = new MutationObserver(() => {
        if (!document.body.contains(iframe)) {
          if (resizeObs) resizeObs.disconnect();
          cleanup.disconnect();
        }
      });
      cleanup.observe(document.body, { childList: true, subtree: true });

      // cid:-Referenzen durch Backend-Proxy ersetzen — URLs vorher signieren (parallel)
      const cids = [...new Set([...htmlToRender.matchAll(/src=["']cid:([^"']+)["']/gi)].map(m => m[1]))];
      if (cids.length) {
        const signedByCid = {};
        await Promise.all(cids.map(async (cid) => {
          try { signedByCid[cid] = await api.inlineImageUrl(full.id, cid); } catch (_) {}
        }));
        htmlToRender = htmlToRender.replace(/src=["']cid:([^"']+)["']/gi, (m, cid) =>
          signedByCid[cid] ? `src="${signedByCid[cid]}"` : m
        );
      }
      _activeIframe = iframe;
      _activeIframeBaseHtml = htmlToRender;
      iframe.srcdoc = _withZoom(htmlToRender);
      body.innerHTML = '';
      body.style.display = 'flex';
      body.appendChild(iframe);
    } else {
      _activeIframe = null;
      _activeIframeBaseHtml = null;
      body.style.zoom = _iframeZoom;
      body.innerHTML = text ? linkify(escHtml(text)) : '<em style="color:#999">Kein Inhalt</em>';
    }

    // Anhänge laden und anzeigen
    await loadAttachments(full);

    if (isDraft) {
      const draftTo      = (full.to_emails || []).join(', ');
      const draftSubject = full.subject || '';

      // "Bearbeiten" — bestehenden Draft bearbeiten, kein neuer Draft
      document.getElementById('btn-edit-draft').onclick = () => {
        _editingDraftItemEl = itemEl;
        openCompose({ to: draftTo, subject: draftSubject, body: text, existingDraftId: email.id });
      };

      // "Senden" — neue Compose mit vorausgefüllten Feldern (eigener neuer Draft)
      document.getElementById('btn-send-draft').onclick = () =>
        openCompose({ to: draftTo, subject: draftSubject, body: text });
    } else {
      // Antworten-Button
      const replyTo = full.reply_to || full.from_email || '';
      const replyToFromEmail = (full.reply_to && full.reply_to !== full.from_email) ? full.from_email : null;
      const replySubject = (full.subject || '').startsWith('Re:')
        ? full.subject : `Re: ${full.subject || ''}`;
      state._currentReplyOpts = { to: replyTo, subject: replySubject, quote: text, quoteHtml: full.body_html || '', replyToEmailId: email.id, replyToFromEmail };
      document.getElementById('btn-reply').onclick = () =>
        openCompose({ to: replyTo, subject: replySubject, quote: text, quoteHtml: full.body_html || '', replyToEmailId: email.id, replyToFromEmail });

      // Weiterleiten-Button
      const fwdSubject = (full.subject || '').startsWith('Fwd:')
        ? full.subject : `Fwd: ${full.subject || ''}`;
      document.getElementById('btn-forward').onclick = () =>
        openCompose({ to: '', subject: fwdSubject, quote: text, quoteHtml: full.body_html || '' });

      // Read-Toggle-Button aktualisieren
      updateReadToggle(email, itemEl);

      // KI-Suggest-Handler
      if (state.kiModeActive) {
        kiSuggestBtn.onclick = () => _runKiSuggest(kiSuggestBtn, []);
      }
    }

    // Synchronisieren-Button (nur Drafts)
    if (isDraft) {
      const btnSync = document.getElementById('btn-sync-draft');
      btnSync.onclick = async () => {
        const origText = btnSync.textContent;
        btnSync.disabled = true;
        btnSync.textContent = 'Synchronisiert…';
        try {
          await api.syncDraft(email.id);
          btnSync.textContent = 'Synchronisiert ✓';
          setTimeout(() => { btnSync.textContent = origText; btnSync.disabled = false; }, 2000);
        } catch (e) {
          btnSync.textContent = 'Fehler: ' + e.message;
          setTimeout(() => { btnSync.textContent = origText; btnSync.disabled = false; }, 3000);
        }
      };
    }

    // Löschen-Button (immer sichtbar)
    document.getElementById('btn-delete').onclick = () => deleteEmail(email, itemEl);
    document.getElementById('btn-spam').onclick       = () => spamEmail(email, itemEl);
    document.getElementById('btn-spam-block').onclick = () => spamEmail(email, itemEl, { blockSender: true });
  } catch (e) {
    body.textContent = 'Fehler beim Laden.';
  }
}

function spamEmail(email, itemEl, opts = {}) {
  const next = itemEl.nextElementSibling || itemEl.previousElementSibling;
  const wasUnread = !email.is_read;

  // Sofort aus DOM und State entfernen (Optimistic UI)
  if (wasUnread) _adjustFolderCount(email.account, email.folder, -1);
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
  api.spamEmail(email.id, opts).then(() => {
    if (opts.blockSender || opts.blockDomain) loadSpamRulesCount();
  }).catch(e => {
    state.emails = [email, ...state.emails];
    renderEmails(true);
    if (wasUnread) _adjustFolderCount(email.account, email.folder, +1);
    _saveToCache();
    alert('Spam-Verschiebung fehlgeschlagen: ' + e.message);
  });
}

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

function updateReadToggle(email, itemEl) {
  const btn = document.getElementById('btn-toggle-read');
  btn.textContent = email.is_read ? 'Als ungelesen markieren' : 'Als gelesen markieren';
  btn.onclick = async () => {
    const newState = !email.is_read;
    email.is_read = newState;
    itemEl.classList.toggle('unread', !newState);
    updateReadToggle(email, itemEl);
    _adjustFolderCount(email.account, email.folder, newState ? -1 : +1);
    try {
      await (newState ? api.markRead(email.id) : api.markUnread(email.id));
    } catch (_) {
      email.is_read = !newState;
      itemEl.classList.toggle('unread', newState);
      updateReadToggle(email, itemEl);
      _adjustFolderCount(email.account, email.folder, newState ? +1 : -1);
    }
  };
}

function linkify(text) {
  return text.replace(
    /(https?:\/\/[^\s<>"]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
  );
}

function showEmpty() {
  document.getElementById('detail-header').style.display = 'none';
  document.getElementById('detail-actions').style.display = 'none';
  document.getElementById('detail-ki-bar').style.display = 'none';
  document.getElementById('detail-body').style.display = 'none';
  document.getElementById('detail-attachments').style.display = 'none';
  document.getElementById('detail-empty').style.display = 'flex';
}

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

// ── Compose-Toolbar ──────────────────────────────────────────
function setupComposeToolbar() {
  // mousedown statt click, damit der Editor nicht die Fokussierung verliert
  document.getElementById('tb-bold').addEventListener('mousedown', (e) => {
    e.preventDefault();
    document.execCommand('bold', false, null);
    document.getElementById('ci-body').focus();
  });
  document.getElementById('tb-underline').addEventListener('mousedown', (e) => {
    e.preventDefault();
    document.execCommand('underline', false, null);
    document.getElementById('ci-body').focus();
  });
  document.querySelectorAll('#tb-fontsize-group .tbfs').forEach(btn => {
    btn.addEventListener('mousedown', (e) => {
      e.preventDefault(); // Fokus im Editor behalten
    });
    btn.addEventListener('click', (e) => {
      document.execCommand('fontSize', false, btn.dataset.size);
      document.getElementById('ci-body').focus();
      document.querySelectorAll('#tb-fontsize-group .tbfs').forEach(b => b.classList.remove('tbfs-active'));
      btn.classList.add('tbfs-active');
    });
  });

  // Link-Button: Selektion in <a> wickeln, ohne Selektion: <a>URL</a> an Cursor
  const linkBtn = document.getElementById('tb-link');
  if (linkBtn) {
    let _savedRange = null;
    linkBtn.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const sel = window.getSelection();
      _savedRange = (sel && sel.rangeCount > 0) ? sel.getRangeAt(0).cloneRange() : null;
    });
    linkBtn.addEventListener('click', () => {
      const body = document.getElementById('ci-body');
      const raw = prompt('Link-URL eingeben:', 'https://');
      if (!raw) { body.focus(); return; }
      const trimmed = raw.trim();
      const finalUrl = (/^(https?:\/\/|mailto:|tel:)/i.test(trimmed))
        ? trimmed
        : 'https://' + trimmed;

      body.focus();
      const sel = window.getSelection();
      if (_savedRange) {
        sel.removeAllRanges();
        sel.addRange(_savedRange);
      }
      const range = (sel && sel.rangeCount > 0) ? sel.getRangeAt(0) : null;

      const a = document.createElement('a');
      a.href = finalUrl;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';

      if (range && !range.collapsed && body.contains(range.commonAncestorContainer)) {
        a.appendChild(range.extractContents());
        range.insertNode(a);
      } else {
        a.textContent = finalUrl;
        if (range && body.contains(range.commonAncestorContainer)) {
          range.insertNode(a);
        } else {
          body.appendChild(a);
        }
      }
      // Cursor hinter den Link setzen
      const after = document.createRange();
      after.setStartAfter(a);
      after.collapse(true);
      sel.removeAllRanges();
      sel.addRange(after);
      _savedRange = null;
    });
  }

  // Sans-Serif-Button: alle font-family-Styles und <font face>-Reste im Body entfernen
  const sansBtn = document.getElementById('tb-sansserif');
  if (sansBtn) {
    sansBtn.addEventListener('mousedown', (e) => e.preventDefault());
    sansBtn.addEventListener('click', () => {
      const body = document.getElementById('ci-body');
      body.querySelectorAll('[style*="font-family" i], [style*="font:" i]').forEach(el => {
        el.style.fontFamily = '';
        if (!el.getAttribute('style')) el.removeAttribute('style');
      });
      body.querySelectorAll('font[face]').forEach(el => {
        el.removeAttribute('face');
      });
      body.focus();
    });
  }

  // Emoji-Buttons: fügen das Emoji als großes <span> am Cursor ein
  document.querySelectorAll('#tb-emoji-group .tb-emoji').forEach(btn => {
    btn.addEventListener('mousedown', (e) => e.preventDefault());
    btn.addEventListener('click', () => {
      const body = document.getElementById('ci-body');
      const span = document.createElement('span');
      span.style.fontSize = '48px';
      span.style.lineHeight = '1';
      span.textContent = btn.dataset.emoji;

      const sel = window.getSelection();
      const range = (sel && sel.rangeCount > 0 && body.contains(sel.anchorNode))
        ? sel.getRangeAt(0)
        : null;

      if (range) {
        range.deleteContents();
        range.insertNode(span);
      } else {
        body.appendChild(span);
      }
      // Cursor hinter den Span setzen, damit nachfolgender Text wieder normal groß ist
      const after = document.createRange();
      after.setStartAfter(span);
      after.collapse(true);
      sel.removeAllRanges();
      sel.addRange(after);
      body.focus();
    });
  });
}
// ─────────────────────────────────────────────────────────────

// ── Bestätigungs-Dialog ──────────────────────────────────────
function confirmDiscard(msg) {
  return new Promise(resolve => {
    document.getElementById('confirm-msg').textContent = msg;
    const overlay = document.getElementById('confirm-overlay');
    overlay.style.display = 'flex';
    const cleanup = (result) => {
      overlay.style.display = 'none';
      resolve(result);
    };
    document.getElementById('confirm-ok').onclick     = () => cleanup(true);
    document.getElementById('confirm-cancel').onclick = () => cleanup(false);
  });
}

function composeHasContent() {
  if (document.getElementById('detail-tabs').style.display === 'none') return false;
  const body    = document.getElementById('ci-body').innerText || '';
  const to      = _toField.getAddresses().join(', ');
  const subject = document.getElementById('ci-subject').value.trim();
  // Hat der Nutzer etwas eingegeben? Signatur alleine zählt nicht als Inhalt.
  const sig = (() => {
    const acc = state.accounts.find(a => a.id === state.activeAccount);
    return acc && acc.signature ? acc.signature.trim() : '';
  })();
  const bodyWithoutSig = body.replace(sig, '').trim();
  return !!(to || subject || bodyWithoutSig);
}
// ─────────────────────────────────────────────────────────────

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

// ── Inline Compose ──────────────────────────────────────────
let _draftId = null;
let _draftTimer = null;
let _editingDraftItemEl = null; // DOM-Element des Draft-Eintrags in der Liste (für Refresh)
let _composeAttachments = [];   // [{id, filename, size}] — temporäre Uploads
let _replyToEmailId = null;     // ID der E-Mail, auf die geantwortet wird (für is_answered)

function showTab(tab) {
  const isCompose = tab === 'compose';
  document.getElementById('view-mode').style.display   = isCompose ? 'none' : 'flex';
  document.getElementById('compose-mode').style.display = isCompose ? 'flex' : 'none';
  document.getElementById('tab-email').classList.toggle('active', !isCompose);
  document.getElementById('tab-compose').classList.toggle('active', isCompose);
}

async function openCompose({ to = '', subject = '', body = null, quote = '', quoteHtml = '', fromAccountId = null, existingDraftId = null, replyToEmailId = null, replyToFromEmail = null } = {}) {
  // Wenn bereits ein Entwurf mit Inhalt offen ist → nachfragen
  if (composeHasContent()) {
    const discard = await confirmDiscard(
      'Du hast einen offenen Entwurf. Wenn du fortfährst, geht er verloren.'
    );
    if (!discard) {
      showTab('compose'); // zurück zum offenen Entwurf
      return false;  // Abgebrochen
    }
  }

  // Tab-Leiste einblenden und benennen
  const tabsEl = document.getElementById('detail-tabs');
  tabsEl.style.display = 'flex';
  document.getElementById('tab-compose').textContent = subject || 'Neue E-Mail';

  showTab('compose');

  // Von-Dropdown befüllen
  const fromSel = document.getElementById('ci-from-account');
  fromSel.innerHTML = state.accounts.map(a =>
    `<option value="${a.id}">${escHtml(a.from_name || a.name || a.from_email)} &lt;${escHtml(a.from_email)}&gt;</option>`
  ).join('');
  fromSel.value = fromAccountId || state.activeAccount || (state.accounts[0]?.id ?? '');

  // SMTP-Dropdown befüllen (ggf. nachladen wenn beim Start fehlgeschlagen)
  if (state.smtpServers.length === 0) await loadSmtpServers();
  const smtpSel = document.getElementById('ci-smtp-server');
  if (state.smtpServers.length > 0) {
    smtpSel.innerHTML = state.smtpServers.map(s =>
      `<option value="${s.id}">${escHtml(s.name)}</option>`
    ).join('');
    const defaultSmtp = state.smtpServers.find(s => s.is_default) || state.smtpServers[0];
    smtpSel.value = defaultSmtp.id;
  } else {
    smtpSel.innerHTML = '<option value="">— kein SMTP-Server —</option>';
  }

  _toField.setAddresses(to ? [to] : []);
  _ccField.clear();
  document.getElementById('ci-subject').value = subject;

  const replytoWarn = document.getElementById('ci-replyto-warning');
  if (replyToFromEmail) {
    replytoWarn.textContent = `Hinweis: Diese E-Mail wird an die Reply-To-Adresse gesendet (${to}), nicht an die Absenderadresse (${replyToFromEmail}).`;
    replytoWarn.style.display = 'block';
  } else {
    replytoWarn.textContent = '';
    replytoWarn.style.display = 'none';
  }

  // Body: explizit übergeben (bei Draft-Bearbeitung) oder Signatur einsetzen (neue E-Mail)
  const account = state.accounts.find(a => a.id === state.activeAccount);
  const sig = account && account.signature ? account.signature.trim() : '';
  const bodyEl = document.getElementById('ci-body');
  if (body !== null) {
    // Plain-Text-Body (aus Draft) → als HTML setzen
    bodyEl.innerHTML = escHtml(body).replace(/\n/g, '<br>');
  } else {
    // Neue E-Mail: Signatur als HTML
    const sigHtml = sig
      ? `<br><br><span>${escHtml(sig).replace(/\n/g, '<br>')}</span>`
      : '';
    bodyEl.innerHTML = sigHtml;
  }

  const quoteEl = document.getElementById('ci-quote');
  quoteEl.classList.remove('has-html');
  delete quoteEl.dataset.quoteHtml;
  if (quoteHtml) {
    // HTML-Dokument auf Body-Inhalt reduzieren, Scripts entfernen
    const _bodyMatch = quoteHtml.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
    const _htmlContent = (_bodyMatch ? _bodyMatch[1] : quoteHtml)
      .replace(/<script\b[\s\S]*?<\/script>/gi, '');
    quoteEl.innerHTML = _htmlContent;
    quoteEl.dataset.quoteHtml = quoteHtml;
    quoteEl.classList.add('has-html');
    quoteEl.style.display = 'block';
  } else if (quote) {
    quoteEl.textContent = quote;
    quoteEl.style.display = 'block';
  } else {
    quoteEl.innerHTML = '';
    quoteEl.style.display = 'none';
  }

  const statusEl = document.getElementById('draft-status');
  statusEl.textContent = '';
  statusEl.style.color = '';
  document.getElementById('btn-send-inline').disabled = false;
  _draftId = existingDraftId; // null → neuer Draft per POST; ID → bestehender Draft per PATCH
  _replyToEmailId = replyToEmailId;

  // Anhang-Liste zurücksetzen (bestehende Uploads aus Speicher löschen)
  _composeAttachments.forEach(a => api.deleteUpload(a.id).catch(() => {}));
  _composeAttachments = [];
  _renderComposeAttachments();

  // Cancel-Button-Text je nach Modus
  const cancelBtn = document.getElementById('btn-compose-cancel');
  cancelBtn.textContent = existingDraftId ? 'Draft speichern' : 'Abbrechen';

  // Cursor an den Anfang des Bodys setzen (Chrome springt sonst ans Ende)
  bodyEl.focus();
  try {
    const range = document.createRange();
    range.setStart(bodyEl, 0);
    range.collapse(true);
    const sel = window.getSelection();
    if (sel) { sel.removeAllRanges(); sel.addRange(range); }
  } catch (_) {}
  // Bei neuer E-Mail (kein Empfänger gesetzt): Fokus ins To-Feld.
  // Bei Antwort/Draft mit Empfänger: Fokus bleibt im Body.
  if (!to) {
    document.getElementById('ci-to-input').focus();
  }
  // Nach dem Rendern an den Anfang scrollen (requestAnimationFrame überschreibt Browser-Autoscroll)
  requestAnimationFrame(() => {
    document.getElementById('compose-mode').scrollTop = 0;
  });

  // KI-Refinement-Bar rendern (nur sichtbar wenn KI-Modus aktiv)
  renderKiRefineBar();

  // Tab im Subject-Feld → direkt ins Body-Feld springen
  const subjectEl = document.getElementById('ci-subject');
  const subjectTabHandler = (e) => {
    if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault();
      document.getElementById('ci-body').focus();
    }
  };
  subjectEl.removeEventListener('keydown', subjectEl._tabHandler);
  subjectEl._tabHandler = subjectTabHandler;
  subjectEl.addEventListener('keydown', subjectTabHandler);

  // Tab im To-Feld → CC überspringen, direkt ins Betreff-Feld.
  // (Tab im Betreff springt dann weiter ins Body-Feld.)
  // Läuft NACH dem Address-Field-Handler (Autocomplete/Chip-Logik bleibt erhalten).
  const toInputEl = document.getElementById('ci-to-input');
  const toTabHandler = (e) => {
    if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault();
      // Wichtig für Text-Expander (z.B. Rocket Typist): Der Empfängertext muss
      // synchron als Chip übernommen werden, bevor der Fokus ins Betreff-Feld
      // springt. Nur auf blur zu warten ist bei schnell folgenden Tab/Text-
      // Events zu spät und kann das erste Snippet-Feld verlieren.
      _toField.commitPending();
      scheduleDraftSave();
      document.getElementById('ci-subject').focus();
    }
  };
  toInputEl.removeEventListener('keydown', toInputEl._tabHandler);
  toInputEl._tabHandler = toTabHandler;
  toInputEl.addEventListener('keydown', toTabHandler);

  // Auto-Save beim Tippen (alte Listener zuerst entfernen, damit keine Duplikate entstehen)
  ['ci-to-input', 'ci-cc-input', 'ci-subject'].forEach(id => {
    const el = document.getElementById(id);
    el.removeEventListener('input', scheduleDraftSave);
    el.addEventListener('input', scheduleDraftSave);
  });
  bodyEl.removeEventListener('input', scheduleDraftSave);
  bodyEl.addEventListener('input', scheduleDraftSave);

  // Bestehender Draft: kein sofortiger Save (Draft existiert bereits, wird beim Tippen per PATCH aktualisiert)
  // Neue Compose: sofort als Draft anlegen
  if (!existingDraftId) {
    await saveDraft();
  }
}

async function closeCompose() {
  clearTimeout(_draftTimer);

  // Wenn ein bestehender Draft bearbeitet wurde: jetzt final speichern und Liste aktualisieren
  if (_draftId && _editingDraftItemEl) {
    await saveDraft();
    // Draft-Eintrag in der Liste und im State mit neuen Daten aktualisieren
    try {
      const updated = await api.getEmail(_draftId);
      const stateEmail = state.emails.find(e => e.id === _draftId);
      if (stateEmail) {
        stateEmail.subject = updated.subject;
        stateEmail.snippet = updated.snippet;
        // Listeneintrag-Text aktualisieren
        const subjectEl = _editingDraftItemEl.querySelector('.email-subject');
        const snippetEl = _editingDraftItemEl.querySelector('.email-snippet');
        if (subjectEl) subjectEl.textContent = updated.subject || '(kein Betreff)';
        if (snippetEl) snippetEl.textContent = updated.snippet || '';
      }
      // Detail-Panel mit aktualisiertem Inhalt öffnen
      openEmail(stateEmail || updated, _editingDraftItemEl);
    } catch (_) {}
    _editingDraftItemEl = null;
  }

  _replyToEmailId = null;
  const _w = document.getElementById('ci-replyto-warning');
  _w.textContent = '';
  _w.style.display = 'none';
  document.getElementById('btn-compose-cancel').textContent = 'Abbrechen';
  document.getElementById('detail-tabs').style.display = 'none';
  showTab('email');
  // Massenversand-Modus zurücksetzen
  _clearBulkMode();
}

function scheduleDraftSave() {
  clearTimeout(_draftTimer);
  _draftTimer = setTimeout(saveDraft, 1000);
  document.getElementById('draft-status').textContent = 'Nicht gespeichert';
}

async function saveDraft() {
  const to          = _toField.getAddresses().join(', ');
  const cc          = _ccField.getAddresses().join(', ');
  const subject     = document.getElementById('ci-subject').value.trim();
  const ciBody      = document.getElementById('ci-body');
  const body        = ciBody.innerText || '';
  const body_html   = ciBody.innerHTML || '';
  const _quoteEl    = document.getElementById('ci-quote');
  const quote       = _quoteEl.textContent;
  const quote_html  = _quoteEl.dataset.quoteHtml || '';
  const from_account = document.getElementById('ci-from-account').value;

  if (!to && !subject && !body.trim()) return;
  if (!from_account) return;

  const statusEl = document.getElementById('draft-status');
  statusEl.textContent = 'Speichert…';

  try {
    const result = await api.saveDraft({ id: _draftId, to, cc, subject, body, body_html, quote, quote_html, from_account });
    if (result && result.id) _draftId = result.id;
    statusEl.textContent = 'Entwurf gespeichert';
    setTimeout(() => { statusEl.textContent = ''; }, 2000);
  } catch (e) {
    statusEl.textContent = '';
    console.warn('saveDraft fehlgeschlagen:', e.message);
  }
}

document.getElementById('btn-compose-cancel').addEventListener('click', closeCompose);

document.getElementById('btn-send-inline').addEventListener('click', async () => {
  // Draft-Timer sofort stoppen — verhindert dass ein ausstehender Save nach dem Senden feuert
  clearTimeout(_draftTimer);

  // Bulk-Modus zweigt früh ab — verwendet eigene Validierung und Status-UI.
  if (_bulkRecipients.length > 0) {
    await _sendBulk();
    return;
  }

  const to         = _toField.getAddresses().join(', ');
  const cc         = _ccField.getAddresses().join(', ');
  const subject    = document.getElementById('ci-subject').value.trim();
  const ciBodyEl   = document.getElementById('ci-body');
  const body       = (ciBodyEl.innerText || '').trim();
  const body_html  = _linkifyHtml(ciBodyEl.innerHTML || '');
  const _qEl       = document.getElementById('ci-quote');
  const quote      = _qEl.textContent;
  const quote_html = _qEl.dataset.quoteHtml || '';
  const fromAccId  = document.getElementById('ci-from-account').value;
  const smtpId     = document.getElementById('ci-smtp-server').value;
  const statusEl   = document.getElementById('draft-status');

  if (!to || !subject) {
    statusEl.textContent = 'Bitte Empfänger und Betreff ausfüllen.';
    statusEl.style.color = 'var(--danger)';
    return;
  }

  // Beantwortet-Symbol sofort im UI aktualisieren (optimistic)
  const sentReplyToId = _replyToEmailId;
  if (sentReplyToId) {
    const local = state.emails.find(e => e.id === sentReplyToId);
    if (local) local.is_answered = true;
    const itemEl = document.querySelector(`.email-item[data-id="${sentReplyToId}"]`);
    itemEl?.querySelector('.flag-answered')?.classList.add('active');
  }

  const attachment_ids   = _composeAttachments.map(a => a.id);
  const draftIdToDelete  = _draftId;

  // Compose sofort schließen — Versand läuft im Hintergrund weiter
  _composeAttachments = [];
  _editingDraftItemEl = null;
  closeCompose();

  try {
    const res = await api.sendEmail({
      to, cc, subject, body, body_html, quote, quote_html,
      from_account: fromAccId, smtp_server: smtpId,
      attachment_ids, in_reply_to_email_id: sentReplyToId,
      draft_id: draftIdToDelete,
    });
    // Notification-Zeile anlegen (SSE-Event aktualisiert sie später)
    if (res && res.job_id) {
      _addSendNotif(res.job_id, to, subject);
    }
  } catch (e) {
    // Validierungsfehler vom Backend (400/502) — sofort als Fehler anzeigen
    const jobId = 'err-' + Date.now();
    _addSendNotif(jobId, to, subject);
    _handleSendResult({ job_id: jobId, success: false, to, subject, error: e.message });
  }
});
// ────────────────────────────────────────────────────────────

// ── Massenversand ───────────────────────────────────────────
//
// Im Compose-Modus öffnet der Button "Massenversand" ein Modal mit einer
// Textarea für E-Mail-Adressen (eine pro Zeile). Bei Übernahme ersetzt ein
// Banner das normale "An"-Feld. Beim Senden ruft das Frontend
// `/emails/bulk-send` auf; das Backend versendet je Empfänger eine eigene
// Mail mit 5 s Abstand. SSE-Events `send-result` aktualisieren live ein
// Status-Modal mit ✓/✗ pro Adresse — inklusive Retry- und Copy-Funktion.

let _bulkRecipients = [];
let _bulkTracking = null;  // { byJobId: Map<jobId,addr>, byAddr: Map<addr,row>, compose: {...} }
const _EMAIL_RE = /^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$/;

function _parseBulkInput(text) {
  const valid = [], invalid = [], seen = new Set();
  (text || '').split(/[\n,;]+/).forEach(line => {
    const addr = line.trim();
    if (!addr) return;
    const m = addr.match(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/);
    if (!m || !_EMAIL_RE.test(m[0])) { invalid.push(addr); return; }
    const key = m[0].toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    valid.push(addr);
  });
  return { valid, invalid };
}

function _renderBulkBanner() {
  const banner = document.getElementById('ci-bulk-banner');
  const toField = document.getElementById('ci-to-field');
  if (_bulkRecipients.length === 0) {
    banner.style.display = 'none';
    toField.style.display = '';
    return;
  }
  banner.querySelector('.ci-bulk-banner-text').innerHTML =
    `Massenversand aktiv: <strong>${_bulkRecipients.length}</strong> Empfänger`;
  banner.style.display = 'flex';
  toField.style.display = 'none';
}

function _clearBulkMode() {
  _bulkRecipients = [];
  _renderBulkBanner();
}

function _openBulkModal() {
  const overlay  = document.getElementById('bulk-modal-overlay');
  const textarea = document.getElementById('bulk-modal-textarea');
  const errors   = document.getElementById('bulk-modal-errors');
  const info     = document.getElementById('bulk-modal-info');
  textarea.value = _bulkRecipients.join('\n');
  errors.style.display = 'none';
  errors.textContent = '';
  if (info) info.textContent = '';
  overlay.style.display = 'flex';
  setTimeout(() => textarea.focus(), 0);
}

// Resend-Helper: wird vom Aussendungs-Tab aufgerufen. Wechselt zum Inbox-Tab,
// oeffnet Compose mit Subject + body_html aus der Original-Aussendung,
// setzt SMTP wenn moeglich und oeffnet das Bulk-Modal mit den ausgewaehlten
// Empfaengern vorgefuellt — Stefan kann dort SMTP wechseln und absenden.
window.mfComposeResend = {
  async open({ subject, body_html, body_text, recipients, from_account, smtp_server } = {}) {
    if (window.mfTabs?.setActiveTab) {
      window.mfTabs.setActiveTab('inbox');
    }
    const accountId = (from_account && state.accounts.find(a => a.id === from_account))
      ? from_account
      : null;
    const opened = await openCompose({
      subject: subject || '',
      fromAccountId: accountId,
    });
    if (opened === false) return false;
    // Body als HTML setzen (openCompose hat nur Plain-body-Support)
    const bodyEl = document.getElementById('ci-body');
    if (bodyEl && body_html) {
      bodyEl.innerHTML = body_html;
    } else if (bodyEl && body_text) {
      bodyEl.innerHTML = _escHtml(body_text).replace(/\n/g, '<br>');
    }
    // SMTP-Server vorwaehlen falls vorhanden — Stefan kann im Dropdown wechseln
    if (smtp_server) {
      const smtpSel = document.getElementById('ci-smtp-server');
      if (smtpSel && smtpSel.querySelector(`option[value="${smtp_server}"]`)) {
        smtpSel.value = smtp_server;
      }
    }
    // Bulk-Mode aktivieren + Modal sofort oeffnen
    _bulkRecipients = (recipients || []).map(r => String(r).trim()).filter(Boolean);
    _renderBulkBanner();
    _openBulkModal();
    return true;
  },
};

function _closeBulkModal() {
  document.getElementById('bulk-modal-overlay').style.display = 'none';
}

document.getElementById('btn-test-send').addEventListener('click', async () => {
  const a = auth.getAuth();
  const userEmail = a?.record?.email;
  const userName  = a?.record?.name || a?.record?.email || '';
  if (!userEmail) {
    alert('Test-Versand nicht möglich: keine eingeloggte E-Mail-Adresse gefunden.');
    return;
  }
  const subject   = document.getElementById('ci-subject').value.trim();
  const ciBodyEl  = document.getElementById('ci-body');
  const body      = (ciBodyEl.innerText || '').trim();
  const body_html = _linkifyHtml(ciBodyEl.innerHTML || '');
  const fromAccId = document.getElementById('ci-from-account').value;
  const smtpId    = document.getElementById('ci-smtp-server').value;
  const statusEl  = document.getElementById('draft-status');
  if (!subject) {
    statusEl.textContent = 'Bitte Betreff ausfüllen für den Test-Versand.';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  // {{name}} / {{email}} clientseitig durch User-Daten ersetzen — Backend
  // wuerde sonst contacts-Lookup machen und ggf. leeren Namen einsetzen.
  const namePh  = /\{\{\s*name\s*\}\}/g;
  const emailPh = /\{\{\s*email\s*\}\}/g;
  const subjectFilled = subject.replace(namePh, userName).replace(emailPh, userEmail);
  const bodyFilled    = body.replace(namePh, userName).replace(emailPh, userEmail);
  const htmlFilled    = body_html.replace(namePh, userName).replace(emailPh, userEmail);

  const btn = document.getElementById('btn-test-send');
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = 'Sende…';
  try {
    await api.sendEmail({
      to: userEmail,
      subject: '[TEST] ' + subjectFilled,
      body: bodyFilled,
      body_html: htmlFilled,
      from_account: fromAccId,
      smtp_server: smtpId,
      attachment_ids: _composeAttachments.map(a => a.id),
    });
    alert(`Test-E-Mail wurde an ${userEmail} versendet.`);
  } catch (err) {
    alert('Test-Versand fehlgeschlagen: ' + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = origLabel;
  }
});

document.getElementById('btn-bulk').addEventListener('click', _openBulkModal);
document.getElementById('ci-bulk-edit').addEventListener('click', _openBulkModal);
document.getElementById('ci-bulk-clear').addEventListener('click', _clearBulkMode);
document.getElementById('bulk-modal-close').addEventListener('click', _closeBulkModal);
document.getElementById('bulk-modal-cancel').addEventListener('click', _closeBulkModal);
document.getElementById('bulk-modal-overlay').addEventListener('click', (e) => {
  if (e.target.id === 'bulk-modal-overlay') _closeBulkModal();
});

function _setBulkInfo(msg, isError = false) {
  const el = document.getElementById('bulk-modal-info');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = isError ? 'var(--danger)' : 'var(--text2)';
}

async function _bulkAddGroupMembers(group) {
  let members;
  try {
    members = await api.contactGroups.members(group.id);
  } catch (err) {
    _setBulkInfo(`Mitglieder laden fehlgeschlagen: ${err.message || err}`, true);
    return;
  }
  const subscribed = members.filter(m => m.email && !m.unsubscribed);
  const unsubCount = members.length - subscribed.length;
  if (subscribed.length === 0) {
    _setBulkInfo(`Gruppe „${group.name}" hat keine versendbaren Mitglieder${unsubCount ? ` (${unsubCount} unsubscribed)` : ''}.`, true);
    return;
  }
  const textarea = document.getElementById('bulk-modal-textarea');
  const existing = new Set(
    (textarea.value || '').split(/[\n,;]+/).map(l => l.trim().toLowerCase()).filter(Boolean)
  );
  const newAddrs = [];
  let duplicate = 0;
  subscribed.forEach(m => {
    const email = (m.email || '').trim();
    if (!email) return;
    if (existing.has(email.toLowerCase())) { duplicate++; return; }
    newAddrs.push(email);
    existing.add(email.toLowerCase());
  });
  const sep = textarea.value && !textarea.value.endsWith('\n') ? '\n' : '';
  textarea.value = textarea.value + sep + newAddrs.join('\n');
  const parts = [`Gruppe „${group.name}": ${newAddrs.length} ergänzt`];
  if (duplicate)  parts.push(`${duplicate} doppelt`);
  if (unsubCount) parts.push(`${unsubCount} unsubscribed`);
  _setBulkInfo(parts.join(' · '));
}

document.getElementById('bulk-modal-add-group').addEventListener('click', (e) => {
  mfDropdown.open({
    trigger: e.currentTarget,
    searchPlaceholder: 'Gruppe suchen…',
    emptyText: 'Keine Kontakt-Gruppen angelegt.',
    loadItems: async () => {
      const groups = await api.contactGroups.list();
      groups.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
      return groups.map(g => ({
        label: g.name,
        sublabel: g.description || '',
        value: g,
      }));
    },
    onSelect: (item) => _bulkAddGroupMembers(item.value),
  });
});

document.getElementById('bulk-modal-apply').addEventListener('click', () => {
  const textarea = document.getElementById('bulk-modal-textarea');
  const errors   = document.getElementById('bulk-modal-errors');
  const { valid, invalid } = _parseBulkInput(textarea.value);
  if (invalid.length > 0) {
    errors.style.display = 'block';
    errors.textContent = 'Ungültige Adressen:\n' + invalid.join('\n');
    return;
  }
  if (valid.length === 0) {
    errors.style.display = 'block';
    errors.textContent = 'Bitte mindestens eine gültige Adresse eingeben.';
    return;
  }
  _bulkRecipients = valid;
  _renderBulkBanner();
  _closeBulkModal();
});

async function _sendBulk() {
  const subject    = document.getElementById('ci-subject').value.trim();
  const ciBodyEl   = document.getElementById('ci-body');
  const body       = (ciBodyEl.innerText || '').trim();
  const body_html  = _linkifyHtml(ciBodyEl.innerHTML || '');
  const _qEl       = document.getElementById('ci-quote');
  const quote      = _qEl.textContent;
  const quote_html = _qEl.dataset.quoteHtml || '';
  const fromAccId  = document.getElementById('ci-from-account').value;
  const smtpId     = document.getElementById('ci-smtp-server').value;
  const statusEl   = document.getElementById('draft-status');

  if (!subject) {
    statusEl.textContent = 'Bitte Betreff ausfüllen.';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  if (_bulkRecipients.length === 0) {
    statusEl.textContent = 'Massenversand-Liste ist leer.';
    statusEl.style.color = 'var(--danger)';
    return;
  }

  const composeSnapshot = {
    subject, body, body_html, quote, quote_html,
    from_account: fromAccId, smtp_server: smtpId,
    attachment_ids: _composeAttachments.map(a => a.id),
    draft_id: _draftId,
  };
  const recipients = _bulkRecipients.slice();

  _composeAttachments = [];
  _editingDraftItemEl = null;
  closeCompose();

  _bulkTracking = { byJobId: new Map(), byAddr: new Map(), compose: composeSnapshot,
                    bulkSendId: null, pollTimer: null };
  _openBulkStatusModal();
  await _bulkStart(recipients, composeSnapshot);
}

async function _bulkStart(recipients, snapshot) {
  recipients.forEach(addr => _bulkUpsertRow(addr, { status: 'queued', error: null, jobId: null }));
  _bulkUpdateSummary();

  let resp;
  try {
    resp = await api.bulkSendEmail({ ...snapshot, recipients, delay_seconds: 5 });
  } catch (e) {
    recipients.forEach(addr => _bulkUpsertRow(addr, { status: 'error', error: e.message, jobId: null }));
    _bulkFinalize();
    return;
  }
  (resp.jobs || []).forEach(j => {
    _bulkTracking.byJobId.set(j.job_id, j.to);
    _bulkUpsertRow(j.to, { status: 'sending', error: null, jobId: j.job_id });
  });
  _bulkRenderFilteredBanner(resp.filtered_out || []);
  _bulkUpdateSummary();

  // Polling-Fallback gegen SSE-Loss bei Backend-Restart (B15).
  // SSE bleibt der primäre Realtime-Pfad; Polling holt nur verpasste
  // Endstatus-Übergänge aus bulk_sends.recipients[] nach.
  if (resp.bulk_send_id) {
    _bulkTracking.bulkSendId = resp.bulk_send_id;
    _bulkTracking.pollTimer = setInterval(_bulkPollOnce, 5_000);
  }
}

async function _bulkPollOnce() {
  if (!_bulkTracking || !_bulkTracking.bulkSendId) return;
  let rec;
  try {
    rec = await api.bulkSends.get(_bulkTracking.bulkSendId);
  } catch (_) {
    return;  // Netzwerk wackelig — beim nächsten Tick erneut.
  }
  if (!_bulkTracking) return;  // Modal in der Zwischenzeit geschlossen
  const recipients = rec.recipients || [];
  for (const r of recipients) {
    const addr = r.raw || r.email;
    if (!addr) continue;
    const cur = _bulkTracking.byAddr.get(addr);
    const pbStatus = r.status;
    let uiStatus = null;
    let err = null;
    if (pbStatus === 'sent')         uiStatus = 'success';
    else if (pbStatus === 'bounced') { uiStatus = 'error'; err = r.error || 'Bounce'; }
    else if (pbStatus === 'error')   { uiStatus = 'error'; err = r.error || 'Fehler'; }
    if (uiStatus && (!cur || cur.status !== uiStatus)) {
      _bulkUpsertRow(addr, { status: uiStatus, error: err });
    }
  }
  _bulkUpdateSummary();
  const allDone = Array.from(_bulkTracking.byAddr.values())
    .every(r => r.status === 'success' || r.status === 'error');
  if (allDone) _bulkFinalize();
}

function _bulkApplyResult(data) {
  const addr = _bulkTracking.byJobId.get(data.job_id);
  if (!addr) return;
  _bulkUpsertRow(addr, {
    status: data.success ? 'success' : 'error',
    error: data.success ? null : (data.error || 'Unbekannter Fehler'),
    jobId: data.job_id,
  });
  _bulkUpdateSummary();
  const allDone = Array.from(_bulkTracking.byAddr.values())
    .every(r => r.status === 'success' || r.status === 'error');
  if (allDone) _bulkFinalize();
}

function _bulkUpsertRow(addr, patch) {
  const prev = _bulkTracking.byAddr.get(addr) || {};
  _bulkTracking.byAddr.set(addr, { ...prev, ...patch, addr });
  _bulkRenderList();
}

function _bulkRenderList() {
  const listEl = document.getElementById('bulk-status-list');
  // Sortierung: Erfolge oben, dann sending/queued, Fehler ganz unten (zum Rauskopieren).
  const order = { success: 0, sending: 1, queued: 2, error: 3 };
  const rows = Array.from(_bulkTracking.byAddr.values())
    .sort((a, b) => (order[a.status] - order[b.status]) || a.addr.localeCompare(b.addr));
  listEl.innerHTML = rows.map(r => {
    let icon, cls;
    if (r.status === 'success')      { icon = '✓'; cls = 'success'; }
    else if (r.status === 'error')   { icon = '✗'; cls = 'error';   }
    else if (r.status === 'sending') { icon = '⏳'; cls = 'sending'; }
    else                              { icon = '·'; cls = 'queued';  }
    const msg = r.status === 'error' && r.error
      ? `<span class="bulk-status-msg" title="${_escHtml(r.error)}">${_escHtml(r.error)}</span>`
      : '';
    return `<div class="bulk-status-row ${cls}"><span class="bulk-status-icon">${icon}</span><span class="bulk-status-addr">${_escHtml(r.addr)}</span>${msg}</div>`;
  }).join('');
}

function _bulkUpdateSummary() {
  const rows = Array.from(_bulkTracking.byAddr.values());
  const total = rows.length;
  const ok    = rows.filter(r => r.status === 'success').length;
  const err   = rows.filter(r => r.status === 'error').length;
  const pending = total - ok - err;
  const titleEl = document.getElementById('bulk-status-title');
  const sumEl   = document.getElementById('bulk-status-summary');
  if (pending > 0) {
    titleEl.textContent = 'Massenversand läuft…';
    sumEl.innerHTML = `<strong>${ok}</strong> gesendet · <strong>${err}</strong> Fehler · <strong>${pending}</strong> ausstehend (insgesamt ${total})`;
  } else {
    titleEl.textContent = err === 0
      ? 'Massenversand abgeschlossen'
      : `Massenversand mit ${err} Fehler${err === 1 ? '' : 'n'} beendet`;
    sumEl.innerHTML = `<strong>${ok}</strong> gesendet · <strong>${err}</strong> Fehler (insgesamt ${total})`;
  }
}

function _openBulkStatusModal() {
  const panel = document.getElementById('bulk-status-panel');
  panel.style.display = 'flex';
  panel.classList.remove('minimized');
  document.getElementById('bulk-status-close').style.display = 'none';
  document.getElementById('bulk-status-done').style.display = 'none';
  document.getElementById('bulk-status-retry').style.display = 'none';
  document.getElementById('bulk-status-copy-failed').style.display = 'none';
  const filtered = document.getElementById('bulk-status-filtered');
  if (filtered) { filtered.style.display = 'none'; filtered.innerHTML = ''; }
}

function _bulkRenderFilteredBanner(filteredOut) {
  const el = document.getElementById('bulk-status-filtered');
  if (!el) return;
  if (!filteredOut || filteredOut.length === 0) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  const bouncedN = filteredOut.filter(f => f.reason === 'bounced').length;
  const unsubN   = filteredOut.filter(f => f.reason === 'unsubscribed').length;
  const parts = [];
  if (bouncedN) parts.push(`<strong>${bouncedN}</strong> bouncte`);
  if (unsubN)   parts.push(`<strong>${unsubN}</strong> unsubscribed`);
  const emails = filteredOut.map(f => f.email).join(', ');
  el.innerHTML = `⚠ ${parts.join(' + ')} Adresse${filteredOut.length === 1 ? '' : 'n'} rausgefiltert: <span title="${_escHtml(emails)}">${_escHtml(emails.length > 80 ? emails.slice(0, 80) + '…' : emails)}</span>`;
  el.style.display = 'block';
}

function _bulkFinalize() {
  const hasErrors = Array.from(_bulkTracking.byAddr.values()).some(r => r.status === 'error');
  if (_bulkTracking.pollTimer) {
    clearInterval(_bulkTracking.pollTimer);
    _bulkTracking.pollTimer = null;
  }
  document.getElementById('bulk-status-close').style.display = '';
  document.getElementById('bulk-status-done').style.display = '';
  document.getElementById('bulk-status-retry').style.display = hasErrors ? '' : 'none';
  document.getElementById('bulk-status-copy-failed').style.display = hasErrors ? '' : 'none';
}

function _closeBulkStatus() {
  const panel = document.getElementById('bulk-status-panel');
  panel.style.display = 'none';
  panel.classList.remove('minimized');
  if (_bulkTracking && _bulkTracking.pollTimer) {
    clearInterval(_bulkTracking.pollTimer);
  }
  _bulkTracking = null;
}

function _toggleBulkStatusMinimized() {
  document.getElementById('bulk-status-panel').classList.toggle('minimized');
}

// Header-Klick und Minimize-Button klappen ein/aus; Schließen-/Aktion-Buttons stoppen Bubble.
document.getElementById('bulk-status-header').addEventListener('click', (e) => {
  if (e.target.closest('#bulk-status-close')) return;
  _toggleBulkStatusMinimized();
});
document.getElementById('bulk-status-close').addEventListener('click', (e) => {
  e.stopPropagation();
  _closeBulkStatus();
});
document.getElementById('bulk-status-done').addEventListener('click', _closeBulkStatus);

document.getElementById('bulk-status-copy-failed').addEventListener('click', async () => {
  if (!_bulkTracking) return;
  const failed = Array.from(_bulkTracking.byAddr.values())
    .filter(r => r.status === 'error').map(r => r.addr);
  if (failed.length === 0) return;
  try {
    await navigator.clipboard.writeText(failed.join('\n'));
    const btn = document.getElementById('bulk-status-copy-failed');
    const orig = btn.textContent;
    btn.textContent = 'Kopiert ✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  } catch (_) { /* Clipboard blockiert — still */ }
});

document.getElementById('bulk-status-retry').addEventListener('click', async () => {
  if (!_bulkTracking) return;
  const failed = Array.from(_bulkTracking.byAddr.values())
    .filter(r => r.status === 'error').map(r => r.addr);
  if (failed.length === 0) return;

  // Alte job_ids der fehlgeschlagenen aus Tracking entfernen (sonst kollidieren
  // ggf. verspätete SSE-Events des ersten Laufs mit dem Retry).
  for (const [jobId, addr] of Array.from(_bulkTracking.byJobId.entries())) {
    if (failed.includes(addr)) _bulkTracking.byJobId.delete(jobId);
  }

  document.getElementById('bulk-status-close').style.display = 'none';
  document.getElementById('bulk-status-done').style.display = 'none';
  document.getElementById('bulk-status-retry').style.display = 'none';
  document.getElementById('bulk-status-copy-failed').style.display = 'none';

  // Retry verwendet KEINE alten draft_id/attachment_ids — die wurden beim ersten Lauf konsumiert.
  const snapshot = { ..._bulkTracking.compose, draft_id: null, attachment_ids: [] };
  await _bulkStart(failed, snapshot);
});

document.getElementById('btn-new-email').addEventListener('click', () => {
  openCompose({});
});

document.getElementById('btn-sync').addEventListener('click', async () => {
  _invalidateFolderCache(state.activeAccount, state.activeFolder);
  await api.syncRun();
  await loadEmails(true);
});

// ── Zoom ──────────────────────────────────────────────────────
document.getElementById('btn-zoom').addEventListener('click', () => {
  const idx = ZOOM_LEVELS.indexOf(_iframeZoom);
  _iframeZoom = ZOOM_LEVELS[(idx + 1) % ZOOM_LEVELS.length];
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

// ── Compose-Anhänge ──────────────────────────────────────────
function _renderComposeAttachments() {
  const el = document.getElementById('ci-attachments');
  if (!_composeAttachments.length) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.innerHTML = _composeAttachments.map((a, i) => `
    <span class="ci-att-chip">
      <span>📎 ${escHtml(a.filename)}</span>
      <span style="color:var(--text2);font-size:12px">${formatBytes(a.size)}</span>
      <button class="ci-att-remove" data-i="${i}" title="Entfernen">×</button>
    </span>
  `).join('');
  el.style.display = 'flex';
  el.querySelectorAll('.ci-att-remove').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = +btn.dataset.i;
      const removed = _composeAttachments.splice(idx, 1)[0];
      if (removed) api.deleteUpload(removed.id).catch(() => {});
      _renderComposeAttachments();
    });
  });
}

document.getElementById('btn-attach').addEventListener('click', () => {
  document.getElementById('ci-file-input').click();
});

document.getElementById('ci-file-input').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  await _uploadFiles(files);
});

async function _uploadFiles(files) {
  const statusEl = document.getElementById('draft-status');
  for (const file of files) {
    if (file.size > 25 * 1024 * 1024) {
      statusEl.textContent = `${file.name}: zu groß (max. 25 MB)`;
      statusEl.style.color = 'var(--danger)';
      setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 3000);
      continue;
    }
    try {
      const fd = new FormData();
      fd.append('file', file);
      const result = await api.uploadAttachment(fd);
      _composeAttachments.push({ id: result.id, filename: result.filename, size: result.size });
      _renderComposeAttachments();
    } catch (err) {
      statusEl.textContent = `Upload fehlgeschlagen: ${err.message}`;
      statusEl.style.color = 'var(--danger)';
      setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 3000);
    }
  }
}

// ── Drag & Drop auf die Compose-Area ─────────────────────────
(function initComposeDragDrop() {
  const composeEl = document.getElementById('compose-mode');
  const overlayEl = document.getElementById('compose-drop-overlay');

  // Phase 1: dragenter auf compose-mode → Overlay einblenden (fängt ab hier alle Events)
  composeEl.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    composeEl.classList.add('drag-over');
  });

  // Phase 2: Overlay ist jetzt pointer-events:all und fängt alle weiteren Events ab
  overlayEl.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });

  overlayEl.addEventListener('dragleave', () => {
    composeEl.classList.remove('drag-over');
  });

  overlayEl.addEventListener('drop', async (e) => {
    e.preventDefault();
    composeEl.classList.remove('drag-over');
    const files = Array.from(e.dataTransfer.files || []);
    if (files.length) await _uploadFiles(files);
  });
})();
// ─────────────────────────────────────────────────────────────

// ── Adressfelder mit Chip-Eingabe und Kontakt-Autocomplete ───
function makeAddressField(fieldId, inputId, suggestionsId, defaultPlaceholder) {
  const field = document.getElementById(fieldId);
  const input = document.getElementById(inputId);
  const box   = document.getElementById(suggestionsId);
  let chips   = []; // [{display}]
  let _acTimer = null;
  let _acActive = -1;
  let _acItems  = [];

  function addChip(text) {
    const display = text.trim().replace(/[,;]+$/, '');
    if (!display) return;
    if (chips.some(c => c.display === display)) return; // Duplikat
    chips.push({ display });
    const chip = document.createElement('span');
    chip.className = 'address-chip';
    chip.title = display;
    chip.innerHTML = `<span class="chip-label">${escHtml(display)}</span><button class="chip-remove" type="button">×</button>`;
    chip.querySelector('.chip-remove').addEventListener('click', () => {
      chips = chips.filter(c => c.display !== display);
      chip.remove();
      input.placeholder = chips.length ? '' : defaultPlaceholder;
    });
    field.insertBefore(chip, input);
    input.placeholder = '';
    input.value = '';
  }

  function getAddresses() {
    const result = chips.map(c => c.display);
    const typed = input.value.trim();
    if (typed) result.push(typed);
    return result;
  }

  function setAddresses(arr) {
    field.querySelectorAll('.address-chip').forEach(c => c.remove());
    chips = [];
    (arr || []).filter(Boolean).forEach(a => addChip(a));
    input.value = '';
    input.placeholder = chips.length ? '' : defaultPlaceholder;
  }

  function clear() { setAddresses([]); }

  // Autocomplete
  function renderSuggestions(contacts) {
    _acItems = contacts;
    _acActive = 0;
    if (!contacts.length) { box.classList.remove('open'); return; }
    box.innerHTML = contacts.map((c, i) =>
      `<div class="contact-suggestion${i === 0 ? ' active' : ''}" data-i="${i}">
        ${c.name ? `<span class="cs-name">${escHtml(c.name)}</span>` : ''}
        <span class="cs-email">${escHtml(c.email)}</span>
      </div>`
    ).join('');
    box.querySelectorAll('.contact-suggestion').forEach(row => {
      row.addEventListener('mouseover', () => {
        _acActive = +row.dataset.i;
        box.querySelectorAll('.contact-suggestion').forEach((r, i) =>
          r.classList.toggle('active', i === _acActive));
      });
    });
    box.classList.add('open');
  }

  function closeSuggestions() { box.classList.remove('open'); }

  function pickContact(contact) {
    const display = contact.name
      ? `${contact.name} <${contact.email}>`
      : contact.email;
    addChip(display);
    closeSuggestions();
    _acItems = [];
  }

  box.addEventListener('mousedown', e => {
    const row = e.target.closest('.contact-suggestion');
    if (row) { e.preventDefault(); pickContact(_acItems[+row.dataset.i]); }
  });

  field.addEventListener('click', () => input.focus());

  input.addEventListener('input', () => {
    clearTimeout(_acTimer);
    const q = input.value.trim();
    if (q.length < 1) { closeSuggestions(); return; }
    _acTimer = setTimeout(async () => {
      try {
        const data = await api.searchContacts(q);
        renderSuggestions(data.items || []);
      } catch (_) { closeSuggestions(); }
    }, 200);
  });

  input.addEventListener('keydown', e => {
    if (e.key === ',' || e.key === ';') {
      e.preventDefault();
      const v = input.value.trim();
      if (v) addChip(v);
      closeSuggestions();
      return;
    }
    if (e.key === 'Backspace' && input.value === '' && chips.length > 0) {
      const lastChip = field.querySelector('.address-chip:last-of-type');
      if (lastChip) { lastChip.remove(); chips.pop(); }
      if (!chips.length) input.placeholder = defaultPlaceholder;
      return;
    }
    if (!box.classList.contains('open')) return;
    const rows = box.querySelectorAll('.contact-suggestion');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _acActive = Math.min(_acActive + 1, rows.length - 1);
      rows.forEach((r, i) => r.classList.toggle('active', i === _acActive));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _acActive = Math.max(_acActive - 1, 0);
      rows.forEach((r, i) => r.classList.toggle('active', i === _acActive));
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      if (_acActive >= 0 && _acItems[_acActive]) {
        e.preventDefault();
        pickContact(_acItems[_acActive]);
      } else if (e.key === 'Tab') {
        const v = input.value.trim();
        if (v) { e.preventDefault(); addChip(v); }
        closeSuggestions();
      } else {
        closeSuggestions();
      }
    } else if (e.key === 'Escape') {
      closeSuggestions();
    }
  });

  function commitPending() {
    closeSuggestions();
    // Beim Verlassen/Tabben des Feldes: getippten Text noch als Chip hinzufügen.
    const v = input.value.trim();
    if (v) {
      addChip(v);
      return true;
    }
    return false;
  }

  input.addEventListener('blur', () => {
    setTimeout(() => {
      commitPending();
    }, 150);
  });

  return { getAddresses, setAddresses, clear, commitPending };
}

const _toField = makeAddressField('ci-to-field', 'ci-to-input', 'ci-to-suggestions', 'empfaenger@beispiel.de');
const _ccField = makeAddressField('ci-cc-field', 'ci-cc-input', 'ci-cc-suggestions', '');
// ─────────────────────────────────────────────────────────────

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

// Spam-Rules-Verwaltung (Phase 2)
let _spamRulesCache = [];

function setupSpamRules() {
  const btn = document.getElementById('btn-spam-rules');
  const overlay = document.getElementById('spam-rules-modal-overlay');
  const closeBtn = document.getElementById('spam-rules-modal-close');
  const doneBtn = document.getElementById('spam-rules-modal-done');
  const searchInput = document.getElementById('spam-rules-search');

  if (btn) btn.addEventListener('click', openSpamRulesModal);
  if (closeBtn) closeBtn.addEventListener('click', closeSpamRulesModal);
  if (doneBtn) doneBtn.addEventListener('click', closeSpamRulesModal);
  if (overlay) overlay.addEventListener('click', e => {
    if (e.target === overlay) closeSpamRulesModal();
  });
  if (searchInput) searchInput.addEventListener('input', e => {
    renderSpamRules(_spamRulesCache, e.target.value.toLowerCase());
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && overlay && overlay.style.display !== 'none') {
      closeSpamRulesModal();
    }
  });
}

async function loadSpamRulesCount() {
  try {
    const data = await api.spamRulesList();
    const n = data.totalItems || 0;
    const badge = document.getElementById('spam-rules-count');
    if (!badge) return;
    badge.textContent = String(n);
    badge.style.display = n > 0 ? '' : 'none';
  } catch (_) { /* still */ }
}

async function openSpamRulesModal() {
  const overlay = document.getElementById('spam-rules-modal-overlay');
  const listEl = document.getElementById('spam-rules-list');
  const searchInput = document.getElementById('spam-rules-search');
  const statusEl = document.getElementById('spam-rules-modal-status');
  if (!overlay || !listEl) return;
  overlay.style.display = '';
  if (searchInput) searchInput.value = '';
  if (statusEl) statusEl.textContent = '';
  listEl.innerHTML = '<div class="spam-rules-loading">Lade…</div>';
  try {
    const data = await api.spamRulesList();
    _spamRulesCache = (data.items || []).sort((a, b) => {
      const ka = a.last_hit || a.created || '';
      const kb = b.last_hit || b.created || '';
      return kb.localeCompare(ka);
    });
    renderSpamRules(_spamRulesCache, '');
  } catch (e) {
    listEl.innerHTML = `<div class="spam-rules-empty">Fehler: ${escHtml(e.message)}</div>`;
  }
}

function closeSpamRulesModal() {
  const overlay = document.getElementById('spam-rules-modal-overlay');
  if (overlay) overlay.style.display = 'none';
}

function renderSpamRules(rules, filterText) {
  const listEl = document.getElementById('spam-rules-list');
  if (!listEl) return;
  const filtered = filterText
    ? rules.filter(r => (r.pattern || '').toLowerCase().includes(filterText))
    : rules;
  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="spam-rules-empty">${rules.length === 0
      ? 'Keine geblockten Absender. Über „+ Absender blocken" beim Spam-Markieren wird ein Eintrag angelegt.'
      : 'Kein Treffer für den Filter.'}</div>`;
    return;
  }
  listEl.innerHTML = '';
  filtered.forEach(rule => {
    const row = document.createElement('div');
    row.className = 'spam-rule-row';
    row.dataset.id = rule.id;
    const lastHit = rule.last_hit
      ? new Date(rule.last_hit).toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: '2-digit' })
      : '–';
    const hits = rule.hits || 0;
    row.innerHTML = `
      <span class="spam-rule-pattern" title="${escHtml(rule.pattern)}">${escHtml(rule.pattern)}</span>
      <span class="spam-rule-type">${rule.match_type === 'domain' ? 'Domain' : 'E-Mail'}</span>
      <span class="spam-rule-meta" title="Letzter Treffer">${hits}× · ${lastHit}</span>
      <button class="spam-rule-delete" title="Absender wieder erlauben">Entblocken</button>
    `;
    row.querySelector('.spam-rule-delete').addEventListener('click', async () => {
      const statusEl = document.getElementById('spam-rules-modal-status');
      try {
        await api.spamRulesDelete(rule.id);
        _spamRulesCache = _spamRulesCache.filter(r => r.id !== rule.id);
        renderSpamRules(_spamRulesCache, document.getElementById('spam-rules-search').value.toLowerCase());
        if (statusEl) {
          statusEl.textContent = `${rule.pattern} wieder erlaubt`;
          setTimeout(() => { statusEl.textContent = ''; }, 2500);
        }
        loadSpamRulesCount();
      } catch (e) {
        if (statusEl) statusEl.textContent = 'Fehler: ' + e.message;
      }
    });
    listEl.appendChild(row);
  });
}

init();

