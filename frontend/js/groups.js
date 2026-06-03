// Gruppen-Verwaltung im Vorlagen-Tab. Liste links, Detail rechts.
// Detail = Header (Name/Beschreibung/Save/Delete) + Mitglieder-Tabelle + Import-Feld.
// Members-Remove geht ueber /contacts/import mode=remove (kein extra Endpoint noetig).

(function () {
  let _groups = [];          // [{id, name, description, members?: contacts[]}]
  let _selectedId = null;
  let _draftId = null;       // 'new' wenn Gruppe gerade angelegt wird (ohne id)
  let _members = [];         // Members der aktiv ausgewaehlten Gruppe
  let _loaded = false;

  const EMAIL_RE = /^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$/;
  const GROUP_NAME_RE = /^[a-z0-9_-]{1,60}$/;

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function normalizeGroupName(raw) {
    if (!raw) return '';
    return raw.trim().toLowerCase().replace(/\s+/g, '_');
  }

  async function load(force = false) {
    if (_loaded && !force) return;
    try {
      _groups = await api.contactGroups.list();
      _groups.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
      _loaded = true;
      await refreshMemberCounts();
      renderList();
      if (_selectedId) {
        const cur = _groups.find(g => g.id === _selectedId);
        if (cur) await selectGroup(cur);
        else clearDetail();
      }
    } catch (err) {
      console.error('groups load failed:', err);
      const list = document.getElementById('groups-list-items');
      if (list) list.innerHTML = `<div class="groups-error">Laden fehlgeschlagen: ${escapeHtml(err.message || String(err))}</div>`;
    }
  }

  async function refreshMemberCounts() {
    // Members-Counts parallel laden. Bei vielen Gruppen ggf. spaeter durch einen
    // dedizierten Endpoint ersetzen.
    const results = await Promise.allSettled(
      _groups.map(g => api.contactGroups.members(g.id))
    );
    results.forEach((res, i) => {
      _groups[i].memberCount = res.status === 'fulfilled' ? res.value.length : null;
    });
  }

  function renderList() {
    const list = document.getElementById('groups-list-items');
    const empty = document.getElementById('groups-list-empty');
    if (!list) return;
    const searchInput = document.getElementById('groups-search');
    const q = (searchInput?.value || '').trim().toLowerCase();
    const filtered = q
      ? _groups.filter(g => (g.name || '').toLowerCase().includes(q) || (g.description || '').toLowerCase().includes(q))
      : _groups;

    list.innerHTML = '';
    if (filtered.length === 0) {
      if (empty) {
        empty.textContent = _groups.length === 0
          ? 'Noch keine Gruppen. „+ Neu" anklicken.'
          : 'Kein Treffer.';
        empty.style.display = 'block';
      }
      return;
    }
    if (empty) empty.style.display = 'none';

    filtered.forEach(g => {
      const item = document.createElement('button');
      item.className = 'groups-list-item';
      item.classList.toggle('active', g.id === _selectedId);
      item.dataset.id = g.id;
      const count = g.memberCount == null ? '?' : g.memberCount;
      item.innerHTML = `<span class="groups-list-name">${escapeHtml(g.name)}</span><span class="groups-list-count">${count}</span>`;
      item.addEventListener('click', () => onSelect(g));
      list.appendChild(item);
    });
  }

  async function onSelect(g) {
    if (_selectedId === g.id) return;
    _selectedId = g.id;
    _draftId = null;
    renderList();
    await selectGroup(g);
  }

  async function selectGroup(g) {
    showDetailPane(true);
    document.getElementById('group-name-input').value = g.name || '';
    document.getElementById('group-description-input').value = g.description || '';
    document.getElementById('group-import-textarea').value = '';
    clearImportPreview();
    updateImportLock();
    await loadMembers(g.id);
  }

  function showDetailPane(visible) {
    const pane = document.getElementById('groups-detail-pane');
    const empty = document.getElementById('groups-editor-empty');
    if (pane) pane.style.display = visible ? '' : 'none';
    if (empty) empty.style.display = visible ? 'none' : '';
  }

  function clearDetail() {
    _selectedId = null;
    _draftId = null;
    _members = [];
    showDetailPane(false);
  }

  async function loadMembers(groupId) {
    const tbody = document.getElementById('group-members-tbody');
    const emptyEl = document.getElementById('group-members-empty');
    const countEl = document.getElementById('group-members-count');
    if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="groups-loading">Lade…</td></tr>';
    try {
      _members = await api.contactGroups.members(groupId);
    } catch (err) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="4" class="groups-error">Mitglieder laden fehlgeschlagen: ${escapeHtml(err.message || err)}</td></tr>`;
      return;
    }
    _members.sort((a, b) => (a.name || a.email || '').localeCompare(b.name || b.email || ''));
    if (countEl) countEl.textContent = String(_members.length);
    renderMembers();
    // Member-Count im Listen-Eintrag aktualisieren
    const g = _groups.find(x => x.id === groupId);
    if (g) { g.memberCount = _members.length; renderList(); }
    if (emptyEl) emptyEl.style.display = _members.length === 0 ? 'block' : 'none';
  }

  function renderMembers() {
    const tbody = document.getElementById('group-members-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    _members.forEach(m => {
      const tr = document.createElement('tr');
      tr.dataset.id = m.id;
      tr.dataset.email = m.email;
      const bounceBadge = m.bounced ? _renderBounceBadge(m) : '';
      const resetBtn = m.bounced
        ? `<button class="row-btn" data-action="clear-bounce" title="Bounce-Flag zurücksetzen (Adresse wird wieder beliefert)">↺</button>`
        : '';
      tr.innerHTML = `
        <td><input type="checkbox" class="group-member-check"></td>
        <td class="group-member-email">${bounceBadge}${escapeHtml(m.email)}</td>
        <td class="group-member-name">${escapeHtml(m.name || '')}</td>
        <td class="group-member-actions">
          ${resetBtn}
          <button class="row-btn" data-action="remove" title="Aus Gruppe entfernen">✕</button>
        </td>
      `;
      tr.querySelector('[data-action="remove"]').addEventListener('click', () => removeMember(m));
      const clearBtn = tr.querySelector('[data-action="clear-bounce"]');
      if (clearBtn) clearBtn.addEventListener('click', () => clearBounce(m));
      tr.querySelector('.group-member-check').addEventListener('change', updateBulkButton);
      tbody.appendChild(tr);
    });
    updateBulkButton();
  }

  function _renderBounceBadge(m) {
    const date = m.bounced_at ? new Date(m.bounced_at).toLocaleDateString('de-DE') : '';
    const title = `Bounce${date ? ' am ' + date : ''}${m.bounced_reason ? ': ' + m.bounced_reason : ''}`;
    return `<span class="bounce-badge" title="${escapeHtml(title)}">⚠ Bounce</span> `;
  }

  async function clearBounce(m) {
    if (!confirm(`Bounce-Flag für „${m.email}" zurücksetzen?\nDie Adresse wird beim nächsten Massenversand wieder beliefert.`)) return;
    try {
      await api.contacts.clearBounce(m.id);
      await loadMembers(_selectedId);
    } catch (err) {
      alert('Reset fehlgeschlagen: ' + (err.message || err));
    }
  }

  function updateBulkButton() {
    const btn = document.getElementById('group-bulk-remove-btn');
    if (!btn) return;
    const checked = document.querySelectorAll('#group-members-tbody .group-member-check:checked').length;
    btn.style.display = checked > 0 ? '' : 'none';
    btn.textContent = checked > 0 ? `Markierte entfernen (${checked})` : 'Markierte entfernen';
  }

  async function removeMember(m) {
    const group = _groups.find(g => g.id === _selectedId);
    if (!group) return;
    if (!confirm(`„${m.name || m.email}" wirklich aus Gruppe „${group.name}" entfernen?`)) return;
    try {
      const line = `${m.email},,${group.name}`;
      await api.contacts.import(line, 'remove');
      await loadMembers(group.id);
    } catch (err) {
      alert('Entfernen fehlgeschlagen: ' + (err.message || err));
    }
  }

  async function bulkRemoveMembers() {
    const group = _groups.find(g => g.id === _selectedId);
    if (!group) return;
    const checks = Array.from(document.querySelectorAll('#group-members-tbody .group-member-check:checked'));
    if (checks.length === 0) return;
    const emails = checks.map(c => c.closest('tr').dataset.email);
    if (!confirm(`${emails.length} Kontakt(e) wirklich aus Gruppe „${group.name}" entfernen?`)) return;
    const lines = emails.map(e => `${e},,${group.name}`).join('\n');
    try {
      await api.contacts.import(lines, 'remove');
      await loadMembers(group.id);
    } catch (err) {
      alert('Entfernen fehlgeschlagen: ' + (err.message || err));
    }
  }

  function checkAllToggle(e) {
    document.querySelectorAll('#group-members-tbody .group-member-check').forEach(cb => {
      cb.checked = e.target.checked;
    });
    updateBulkButton();
  }

  function onNewGroup() {
    _selectedId = null;
    _draftId = 'new';
    renderList();
    showDetailPane(true);
    document.getElementById('group-name-input').value = '';
    document.getElementById('group-description-input').value = '';
    document.getElementById('group-members-tbody').innerHTML = '';
    document.getElementById('group-members-count').textContent = '0';
    document.getElementById('group-members-empty').style.display = 'none';
    document.getElementById('group-import-textarea').value = '';
    clearImportPreview();
    updateImportLock();
    document.getElementById('group-name-input').focus();
  }

  async function onSave() {
    const nameRaw = document.getElementById('group-name-input').value;
    const name = normalizeGroupName(nameRaw);
    const description = document.getElementById('group-description-input').value.trim();
    if (!GROUP_NAME_RE.test(name)) {
      alert('Name ungültig. Erlaubt: 1–60 Zeichen, nur a-z, 0-9, _, -.');
      return;
    }
    try {
      if (_draftId === 'new') {
        const created = await api.contactGroups.create({ name, description });
        created.memberCount = 0;
        _groups.push(created);
        _groups.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        _selectedId = created.id;
        _draftId = null;
        renderList();
        await selectGroup(created);
      } else if (_selectedId) {
        const updated = await api.contactGroups.update(_selectedId, { name, description });
        const idx = _groups.findIndex(g => g.id === _selectedId);
        if (idx >= 0) { _groups[idx] = { ..._groups[idx], ...updated }; }
        _groups.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        renderList();
        document.getElementById('group-name-input').value = updated.name;
      }
    } catch (err) {
      alert('Speichern fehlgeschlagen: ' + (err.message || err));
    }
  }

  async function onDelete() {
    if (_draftId === 'new') { clearDetail(); return; }
    const g = _groups.find(x => x.id === _selectedId);
    if (!g) return;
    if (!confirm(`Gruppe „${g.name}" wirklich löschen? Die Kontakte bleiben, nur die Zuordnung wird entfernt.`)) return;
    try {
      await api.contactGroups.delete(g.id);
      _groups = _groups.filter(x => x.id !== g.id);
      clearDetail();
      renderList();
    } catch (err) {
      alert('Löschen fehlgeschlagen: ' + (err.message || err));
    }
  }

  // ── Export (Zwischenablage) ───────────────────────────────────────────

  // Kopiert alle E-Mail-Adressen der ausgewaehlten Gruppe in die Zwischenablage,
  // eine pro Zeile — passt direkt ins Massenversand-Modal. Kontakte liegen mit
  // Gruppenzugehoerigkeit in PocketBase (Backup dort), daher kein CSV-Download.
  async function onExportEmails() {
    const btn = document.getElementById('group-export-btn');
    if (_members.length === 0) {
      alert('Diese Gruppe hat keine Mitglieder.');
      return;
    }
    const text = _members.map(m => m.email).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      if (btn) {
        const old = btn.textContent;
        btn.textContent = `✓ ${_members.length} kopiert`;
        btn.disabled = true;
        setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 2000);
      }
    } catch (err) {
      alert('Kopieren fehlgeschlagen: ' + (err.message || err));
    }
  }

  // ── Import-Feld ───────────────────────────────────────────────────────

  function clearImportPreview() {
    const el = document.getElementById('group-import-preview');
    if (el) { el.innerHTML = ''; el.style.display = 'none'; }
    const apply = document.getElementById('group-import-apply-btn');
    if (apply) apply.disabled = true;
    const status = document.getElementById('group-import-status');
    if (status) status.textContent = '';
  }

  // Solange die Gruppe noch nicht gespeichert ist (neuer Entwurf), Import-Buttons
  // sperren und Hinweis anzeigen. "Dann importieren" bleibt auch bei gespeicherter
  // Gruppe gesperrt, bis "Erst prüfen" gültige Zeilen gefunden hat.
  function updateImportLock() {
    const saved = !!_selectedId && _draftId !== 'new';
    const checkBtn = document.getElementById('group-import-check-btn');
    const applyBtn = document.getElementById('group-import-apply-btn');
    const msg = document.getElementById('group-import-locked-msg');
    if (msg) msg.style.display = saved ? 'none' : 'block';
    if (checkBtn) checkBtn.disabled = !saved;
    if (!saved && applyBtn) applyBtn.disabled = true;
  }

  function parseImportLines(raw) {
    const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);
    const valid = [];
    const invalid = [];
    const seen = new Set();
    lines.forEach((line, i) => {
      const parts = line.split(',').map(p => p.trim());
      const emailRaw = parts[0] || '';
      const name = parts[1] || '';
      const extraGroups = (parts[2] || '').split(';').map(g => normalizeGroupName(g)).filter(Boolean);
      const email = emailRaw.toLowerCase();
      if (!email) {
        invalid.push({ lineno: i + 1, raw: line, reason: 'Email leer' });
        return;
      }
      if (!EMAIL_RE.test(email)) {
        invalid.push({ lineno: i + 1, raw: line, reason: 'Email ungültig' });
        return;
      }
      if (seen.has(email)) {
        invalid.push({ lineno: i + 1, raw: line, reason: 'Doppelte Email in Eingabe' });
        return;
      }
      seen.add(email);
      valid.push({ email, name, extraGroups });
    });
    return { valid, invalid };
  }

  function onCheckImport() {
    if (!_selectedId || _draftId === 'new') {
      alert('Bitte zuerst die Gruppe speichern, bevor du Mitglieder importierst.');
      return;
    }
    const group = _groups.find(g => g.id === _selectedId);
    if (!group) return;
    const raw = document.getElementById('group-import-textarea').value;
    if (!raw.trim()) { clearImportPreview(); return; }
    const { valid, invalid } = parseImportLines(raw);
    const previewEl = document.getElementById('group-import-preview');
    const apply = document.getElementById('group-import-apply-btn');
    let html = '';
    html += `<div class="groups-import-summary">${valid.length} gültig · ${invalid.length} ungültig</div>`;
    if (invalid.length > 0) {
      html += '<details class="groups-import-invalid" open><summary>Ungültige Zeilen</summary><ul>';
      invalid.forEach(inv => {
        html += `<li><code>${escapeHtml(inv.raw)}</code> — ${escapeHtml(inv.reason)}</li>`;
      });
      html += '</ul></details>';
    }
    previewEl.innerHTML = html;
    previewEl.style.display = 'block';
    apply.disabled = valid.length === 0;
    apply.dataset.validCount = String(valid.length);
  }

  async function onApplyImport() {
    if (!_selectedId) return;
    const group = _groups.find(g => g.id === _selectedId);
    if (!group) return;
    const raw = document.getElementById('group-import-textarea').value;
    const { valid } = parseImportLines(raw);
    if (valid.length === 0) return;
    // Jede Zeile: aktive Gruppe immer mit anhaengen + ggf. extra-Gruppen
    const lines = valid.map(v => {
      const allGroups = [group.name, ...v.extraGroups.filter(g => g !== group.name)];
      return `${v.email},${v.name},${allGroups.join(';')}`;
    }).join('\n');
    const status = document.getElementById('group-import-status');
    const apply = document.getElementById('group-import-apply-btn');
    apply.disabled = true;
    if (status) status.textContent = 'Importiere…';
    try {
      const res = await api.contacts.import(lines, 'add');
      const c = res.counts || {};
      const msgParts = [];
      if (c.added)     msgParts.push(`${c.added} neu`);
      if (c.updated)   msgParts.push(`${c.updated} aktualisiert`);
      if (c.unchanged) msgParts.push(`${c.unchanged} unverändert`);
      if (c.errors)    msgParts.push(`${c.errors} Fehler`);
      if (status) status.textContent = msgParts.length ? `✓ ${msgParts.join(' · ')}` : '✓ fertig';
      document.getElementById('group-import-textarea').value = '';
      clearImportPreview();
      await loadMembers(group.id);
    } catch (err) {
      if (status) status.textContent = '';
      alert('Import fehlgeschlagen: ' + (err.message || err));
    } finally {
      apply.disabled = false;
    }
  }

  // ── Event-Wiring ─────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', () => {
    const newBtn = document.getElementById('btn-group-new');
    if (newBtn) newBtn.addEventListener('click', onNewGroup);

    const search = document.getElementById('groups-search');
    if (search) search.addEventListener('input', renderList);

    const saveBtn = document.getElementById('group-save-btn');
    if (saveBtn) saveBtn.addEventListener('click', onSave);
    const delBtn = document.getElementById('group-delete-btn');
    if (delBtn) delBtn.addEventListener('click', onDelete);

    const exportBtn = document.getElementById('group-export-btn');
    if (exportBtn) exportBtn.addEventListener('click', onExportEmails);

    const checkAll = document.getElementById('group-members-check-all');
    if (checkAll) checkAll.addEventListener('change', checkAllToggle);
    const bulkBtn = document.getElementById('group-bulk-remove-btn');
    if (bulkBtn) bulkBtn.addEventListener('click', bulkRemoveMembers);

    const checkBtn = document.getElementById('group-import-check-btn');
    if (checkBtn) checkBtn.addEventListener('click', onCheckImport);
    const applyBtn = document.getElementById('group-import-apply-btn');
    if (applyBtn) applyBtn.addEventListener('click', onApplyImport);

    // Detail-Pane standardmaessig versteckt
    showDetailPane(false);

    window.addEventListener('mf:section-changed', (e) => {
      if (e.detail.section === 'groups') load();
    });
    if (document.body.dataset.activeTab === 'templates' &&
        document.getElementById('templates-main')?.dataset.activeSection === 'groups') {
      load();
    }
  });

  window.mfGroups = { load, reload: () => load(true) };
})();
