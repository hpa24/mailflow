// Aussendungs-Historie (bulk_sends). Liste links, Detail rechts mit
// Empfaenger-Tabelle, Status-Filter und Re-Send fuer markierte Empfaenger.

(function () {
  let _list = [];
  let _selected = null;       // full Record incl. recipients
  let _selectedId = null;
  let _activeFilter = 'all';  // all|sent|error|bounced|queued
  let _loaded = false;

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function formatDateTime(iso) {
    if (!iso) return '';
    const d = new Date(iso.replace(' ', 'T') + (iso.endsWith('Z') ? '' : 'Z'));
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString('de-DE', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  }

  async function load(force = false) {
    if (_loaded && !force) return;
    try {
      _list = await api.bulkSends.list(200);
      _loaded = true;
      renderList();
      if (_selectedId) {
        await loadDetail(_selectedId, /*silent*/ true);
      }
    } catch (err) {
      console.error('bulk_sends load failed:', err);
      const items = document.getElementById('bulk-sends-list-items');
      if (items) items.innerHTML = `<div class="snippets-list-empty">Laden fehlgeschlagen: ${escapeHtml(err.message || String(err))}</div>`;
    }
  }

  function renderList() {
    const itemsEl = document.getElementById('bulk-sends-list-items');
    const emptyEl = document.getElementById('bulk-sends-list-empty');
    if (!itemsEl) return;
    const q = (document.getElementById('bulk-sends-search')?.value || '').trim().toLowerCase();
    const filtered = q ? _list.filter(b => (b.subject || '').toLowerCase().includes(q)) : _list;
    itemsEl.innerHTML = '';
    if (filtered.length === 0) {
      if (emptyEl) {
        emptyEl.style.display = 'block';
        emptyEl.textContent = _list.length === 0 ? 'Noch keine Aussendungen.' : 'Kein Treffer.';
      }
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';
    filtered.forEach(b => {
      const btn = document.createElement('button');
      btn.className = 'bulk-sends-list-item';
      btn.classList.toggle('active', b.id === _selectedId);
      const meta = [];
      meta.push(`${b.sent_count || 0}/${b.total_count || 0}`);
      if (b.error_count) meta.push(`<span style="color:#991b1b">${b.error_count} Fehler</span>`);
      if (b.bounced_count) meta.push(`<span class="bs-bounce-badge">${b.bounced_count} Bounce</span>`);
      btn.innerHTML = `
        <div class="bs-subject">${escapeHtml(b.subject || '(ohne Betreff)')}</div>
        <div class="bs-meta">
          <span>${escapeHtml(formatDateTime(b.sent_at) || formatDateTime(b.created))}</span>
          <span>·</span>
          <span>${meta.join(' · ')}</span>
        </div>
      `;
      btn.addEventListener('click', () => onSelect(b.id));
      itemsEl.appendChild(btn);
    });
  }

  async function onSelect(id) {
    if (_selectedId === id) return;
    await loadDetail(id);
  }

  async function loadDetail(id, silent = false) {
    try {
      const rec = await api.bulkSends.get(id);
      _selected = rec;
      _selectedId = id;
      renderList();
      renderDetail();
    } catch (err) {
      if (!silent) alert('Detail laden fehlgeschlagen: ' + (err.message || err));
    }
  }

  function showDetail() {
    document.getElementById('bulk-sends-detail-empty').style.display = 'none';
    document.getElementById('bulk-sends-detail-pane').style.display = 'block';
  }
  function showEmpty() {
    document.getElementById('bulk-sends-detail-empty').style.display = 'block';
    document.getElementById('bulk-sends-detail-pane').style.display = 'none';
  }

  function renderDetail() {
    if (!_selected) { showEmpty(); return; }
    showDetail();
    const r = _selected;
    document.getElementById('bulk-sends-detail-subject').textContent = r.subject || '(ohne Betreff)';
    const meta = [
      formatDateTime(r.sent_at),
      r.from_account_email,
      `${r.total_count || 0} Empfänger`,
      `Abstand ${r.delay_seconds || 0}s`,
    ].filter(Boolean);
    document.getElementById('bulk-sends-detail-meta').textContent = meta.join(' · ');
    renderRecipients();
  }

  function recipients() {
    return (_selected?.recipients) || [];
  }

  function countsByStatus() {
    const c = { all: 0, sent: 0, error: 0, bounced: 0, queued: 0 };
    recipients().forEach(r => {
      c.all++;
      const s = r.status || 'queued';
      if (c[s] != null) c[s]++;
    });
    return c;
  }

  function filterRecipients() {
    if (_activeFilter === 'all') return recipients();
    return recipients().filter(r => (r.status || 'queued') === _activeFilter);
  }

  function renderRecipients() {
    const tbody = document.getElementById('bulk-sends-recipients-tbody');
    if (!tbody) return;
    const counts = countsByStatus();
    document.querySelectorAll('.bsf-count').forEach(el => {
      const k = el.dataset.count;
      el.textContent = counts[k] || 0;
    });
    document.querySelectorAll('.bulk-sends-filter-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.filter === _activeFilter);
    });
    const rows = filterRecipients();
    tbody.innerHTML = rows.map(r => {
      const status = r.status || 'queued';
      const errMsg = status === 'error' ? (r.error || '') :
                     status === 'bounced' ? (r.bounced_reason || r.error || 'Bounce') : '';
      return `
        <tr data-email="${escapeHtml((r.email || '').toLowerCase())}">
          <td><input type="checkbox" class="bs-row-check" data-email="${escapeHtml((r.email || '').toLowerCase())}" ${status === 'bounced' ? 'checked' : ''}></td>
          <td><span class="bs-status ${status}">${statusLabel(status)}</span></td>
          <td>${escapeHtml(r.email || '')}</td>
          <td>${escapeHtml(r.name || '')}</td>
          <td>${errMsg ? `<span class="bs-error-msg" title="${escapeHtml(errMsg)}">${escapeHtml(errMsg.slice(0, 120))}</span>` : ''}</td>
        </tr>
      `;
    }).join('');
    document.getElementById('bulk-sends-check-all').checked = false;
    updateSelectionHint();
  }

  function statusLabel(s) {
    return { sent: 'Erfolgreich', error: 'Fehler', bounced: 'Bounce', queued: 'Ausstehend' }[s] || s;
  }

  function updateSelectionHint() {
    const hint = document.getElementById('bulk-sends-selection-hint');
    if (!hint) return;
    const n = document.querySelectorAll('.bs-row-check:checked').length;
    hint.textContent = n > 0 ? `${n} ausgewählt` : '';
  }

  function selectedRecipients() {
    const checked = Array.from(document.querySelectorAll('.bs-row-check:checked'))
      .map(el => el.dataset.email);
    const set = new Set(checked);
    return recipients().filter(r => set.has((r.email || '').toLowerCase()));
  }

  // ── Re-Send ────────────────────────────────────────────────────
  async function onResend() {
    if (!_selected) return;
    const sel = selectedRecipients();
    if (sel.length === 0) {
      alert('Keine Empfänger ausgewählt. Bouncte sind standardmäßig markiert — Filter wechseln oder Häkchen setzen.');
      return;
    }
    if (!window.mfComposeResend?.open) {
      alert('Compose-Resend nicht initialisiert.');
      return;
    }
    const ok = await window.mfComposeResend.open({
      subject: _selected.subject || '',
      body_html: _selected.body_html || '',
      body_text: _selected.body_text || '',
      from_account: _selected.from_account || null,
      smtp_server: _selected.smtp_server || null,
      recipients: sel.map(r => r.raw || (r.name ? `${r.name} <${r.email}>` : r.email)),
    });
    if (ok === false) return;
  }

  // ── Preview ────────────────────────────────────────────────────
  function onPreview() {
    if (!_selected) return;
    const ov = document.getElementById('bulk-sends-preview-overlay');
    const iframe = document.getElementById('bulk-sends-preview-iframe');
    const html = _selected.body_html || (_selected.body_text ? `<pre>${escapeHtml(_selected.body_text)}</pre>` : '<em>Kein Inhalt</em>');
    iframe.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;padding:16px;font-family:-apple-system,sans-serif;color:#1c1c1e;background:#fff;}</style></head><body>${html}</body></html>`;
    ov.style.display = 'flex';
  }
  function closePreview() {
    document.getElementById('bulk-sends-preview-overlay').style.display = 'none';
  }

  // ── Delete ─────────────────────────────────────────────────────
  async function onDelete() {
    if (!_selected) return;
    if (!confirm(`Aussendung „${_selected.subject || '(ohne Betreff)'}" wirklich aus der Historie löschen?\n\nDie versendeten E-Mails selbst sind davon nicht betroffen.`)) return;
    try {
      await api.bulkSends.delete(_selectedId);
      _list = _list.filter(b => b.id !== _selectedId);
      _selected = null;
      _selectedId = null;
      renderList();
      showEmpty();
    } catch (err) {
      alert('Löschen fehlgeschlagen: ' + (err.message || err));
    }
  }

  function bindGlobal() {
    document.getElementById('bulk-sends-search')?.addEventListener('input', renderList);
    document.getElementById('bulk-sends-resend-btn')?.addEventListener('click', onResend);
    document.getElementById('bulk-sends-preview-btn')?.addEventListener('click', onPreview);
    document.getElementById('bulk-sends-preview-close')?.addEventListener('click', closePreview);
    document.getElementById('bulk-sends-preview-overlay')?.addEventListener('click', (e) => {
      if (e.target.id === 'bulk-sends-preview-overlay') closePreview();
    });
    document.getElementById('bulk-sends-delete-btn')?.addEventListener('click', onDelete);

    document.querySelectorAll('.bulk-sends-filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        _activeFilter = btn.dataset.filter;
        renderRecipients();
      });
    });
    document.getElementById('bulk-sends-check-all')?.addEventListener('change', (e) => {
      document.querySelectorAll('.bs-row-check').forEach(cb => { cb.checked = e.target.checked; });
      updateSelectionHint();
    });
    document.addEventListener('change', (e) => {
      if (e.target.classList.contains('bs-row-check')) updateSelectionHint();
    });

    window.addEventListener('mf:section-changed', (e) => {
      if (e.detail.section === 'bulk_sends') load();
    });
    if (document.body.dataset.activeTab === 'templates' &&
        document.getElementById('templates-main')?.dataset.activeSection === 'bulk_sends') {
      load();
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    bindGlobal();
    showEmpty();
  });

  window.mfBulkSends = { load, reload: () => load(true) };
})();
