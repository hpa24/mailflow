// Spam-Logik: Verschieben einer Mail in den Spam-Ordner + Verwaltung der
// Server-Spam-Rules (geblockte Absender).
//
// Ausgegliedert aus inbox.js im Rahmen von C4 Phase 2. Greift auf globale
// Helper aus inbox.js zu (state, _adjustFolderCount, cleanupThreadStyling,
// openEmail, showEmpty, renderEmails, _saveToCache, escHtml). Da spam.js
// vor inbox.js geladen wird, sind diese Referenzen zur Parse-Zeit nur
// Textsymbole — sie werden erst beim ersten Aufruf aufgelöst, wenn inbox.js
// längst geladen ist.

function spamEmail(email, itemEl, opts = {}) {
  const next = itemEl.nextElementSibling || itemEl.previousElementSibling;
  const wasUnread = !email.is_read;

  // Sofort aus DOM und State entfernen (Optimistic UI)
  if (wasUnread) _adjustFolderCount(email.account, email.folder, -1);
  itemEl.remove();
  state.emails = state.emails.filter(em => em.id !== email.id);
  _addTombstone(email.id);
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
    _clearTombstone(email.id);
    state.emails = [email, ...state.emails];
    renderEmails(true);
    if (wasUnread) _adjustFolderCount(email.account, email.folder, +1);
    _saveToCache();
    alert('Spam-Verschiebung fehlgeschlagen: ' + e.message);
  });
}


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
