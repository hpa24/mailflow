// Variablen-Tabelle im Vorlagen-Tab. Inline-Edit auf value/description per
// Doppelklick. Neue Variable als Draft-Zeile am Tabellen-Anfang.

(function () {
  let _variables = [];
  let _loaded = false;
  let _activePrefix = 'all';

  const RESERVED_NAMES = new Set(['name', 'email']);
  const NAME_RE = /^[a-z_][a-z0-9_]*$/;

  function prefixOf(name) {
    const i = (name || '').indexOf('_');
    return i > 0 ? name.slice(0, i) : null;
  }

  function renderPrefixFilter() {
    const bar = document.getElementById('variables-prefix-filter');
    if (!bar) return;
    const prefixes = Array.from(new Set(
      _variables.map(v => prefixOf(v.name)).filter(Boolean)
    )).sort();
    if (prefixes.length === 0) {
      bar.style.display = 'none';
      bar.innerHTML = '';
      _activePrefix = 'all';
      return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = '';
    const mkBtn = (label, prefix) => {
      const btn = document.createElement('button');
      btn.className = 'var-prefix-btn';
      btn.classList.toggle('active', _activePrefix === prefix);
      btn.textContent = label;
      btn.addEventListener('click', () => {
        _activePrefix = prefix;
        renderPrefixFilter();
        render();
      });
      return btn;
    };
    bar.appendChild(mkBtn('Alle', 'all'));
    prefixes.forEach(p => bar.appendChild(mkBtn(p, p)));
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' });
  }

  async function load(force = false) {
    if (_loaded && !force) return;
    try {
      _variables = await api.variables.list();
      _variables.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
      _loaded = true;
      renderPrefixFilter();
      render();
    } catch (err) {
      console.error('variables load failed:', err);
      const tbody = document.getElementById('variables-tbody');
      if (tbody) tbody.innerHTML = `<tr><td colspan="4" class="var-error">Laden fehlgeschlagen: ${escapeHtml(err.message || String(err))}</td></tr>`;
    }
  }

  function visible() {
    if (_activePrefix === 'all') return _variables;
    return _variables.filter(v => prefixOf(v.name) === _activePrefix);
  }

  function render() {
    const tbody = document.getElementById('variables-tbody');
    const empty = document.getElementById('variables-empty');
    if (!tbody) return;
    tbody.innerHTML = '';
    const list = visible();
    if (list.length === 0) {
      if (empty) {
        empty.style.display = 'block';
        empty.textContent = _variables.length === 0
          ? 'Noch keine Variablen angelegt. Mit „+ Neue Variable" anfangen.'
          : `Keine Variablen mit Präfix „${_activePrefix}".`;
      }
      return;
    }
    if (empty) empty.style.display = 'none';
    list.forEach(v => tbody.appendChild(renderRow(v)));
  }

  function renderRow(v) {
    const tr = document.createElement('tr');
    tr.dataset.id = v.id;
    tr.innerHTML = `
      <td class="var-name" data-field="name" title="Doppelklick zum Umbenennen"></td>
      <td class="var-value" data-field="value"></td>
      <td class="var-updated">${escapeHtml(formatDate(v.updated))}</td>
      <td class="var-actions">
        <button class="row-btn" data-action="delete" title="Löschen">✕</button>
      </td>
    `;
    tr.querySelector('[data-field="name"]').innerHTML = `<code>{{${escapeHtml(v.name)}}}</code>`;
    tr.querySelector('[data-field="value"]').textContent = v.value || '';

    tr.querySelectorAll('[data-field]').forEach(td => {
      td.addEventListener('dblclick', () => startEdit(tr, td, v));
    });
    tr.querySelector('[data-action="delete"]').addEventListener('click', () => onDelete(v));
    return tr;
  }

  function startEdit(tr, td, v) {
    if (td.querySelector('input')) return;
    const field = td.dataset.field;
    const oldValue = v[field] || '';
    const input = document.createElement('input');
    input.type = 'text';
    input.value = oldValue;
    input.className = 'var-inline-input';
    td.innerHTML = '';
    td.appendChild(input);
    input.focus();
    input.select();

    function restoreDisplay(value) {
      if (field === 'name') {
        td.innerHTML = `<code>{{${escapeHtml(value)}}}</code>`;
      } else {
        td.textContent = value;
      }
    }

    let done = false;
    async function commit() {
      if (done) return;
      done = true;
      const newValue = input.value.trim();
      if (newValue === oldValue) {
        restoreDisplay(oldValue);
        return;
      }
      if (field === 'name') {
        await commitNameRename(tr, td, v, oldValue, newValue.toLowerCase(), restoreDisplay);
        return;
      }
      try {
        const updated = await api.variables.update(v.id, { [field]: newValue });
        v[field] = updated[field] != null ? updated[field] : newValue;
        v.updated = updated.updated || new Date().toISOString();
        restoreDisplay(v[field]);
        tr.querySelector('.var-updated').textContent = formatDate(v.updated);
      } catch (err) {
        alert('Speichern fehlgeschlagen: ' + (err.message || err));
        restoreDisplay(oldValue);
      }
    }
    function cancel() {
      if (done) return;
      done = true;
      restoreDisplay(oldValue);
    }

    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
  }

  async function commitNameRename(tr, td, v, oldName, newName, restoreDisplay) {
    if (RESERVED_NAMES.has(newName)) {
      alert(`„${newName}" ist für Kontakt-Felder reserviert. Bitte anderen Namen wählen.`);
      restoreDisplay(oldName);
      return;
    }
    if (!NAME_RE.test(newName)) {
      alert('Name ungültig — nur Kleinbuchstaben, Ziffern, Unterstriche; Start mit Buchstabe oder _');
      restoreDisplay(oldName);
      return;
    }

    let usage;
    try {
      usage = await api.variables.usage(v.id);
    } catch (err) {
      alert('Verwendungs-Prüfung fehlgeschlagen: ' + (err.message || err));
      restoreDisplay(oldName);
      return;
    }
    const refCount = (usage?.templates?.length || 0) + (usage?.snippets?.length || 0);

    let replace_in_usage = false;
    if (refCount > 0) {
      const decision = await mfRenameGuard.show({
        kind: 'Variable', oldName, newName, usage,
      });
      if (decision === 'cancel') {
        restoreDisplay(oldName);
        return;
      }
      replace_in_usage = (decision === 'replace');
    }

    try {
      const result = await api.variables.rename(v.id, { new_name: newName, replace_in_usage });
      v.name = result.new_name;
      v.updated = new Date().toISOString();
      restoreDisplay(v.name);
      tr.querySelector('.var-updated').textContent = formatDate(v.updated);
      _variables.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
      renderPrefixFilter();
      render();
      if (replace_in_usage) {
        const parts = [];
        if (result.replaced_templates) parts.push(`${result.replaced_templates} Vorlage(n)`);
        if (result.replaced_snippets) parts.push(`${result.replaced_snippets} Snippet(s)`);
        if (parts.length) console.info(`Variable umbenannt — Refs aktualisiert in ${parts.join(' + ')}`);
      }
    } catch (err) {
      alert('Umbenennen fehlgeschlagen: ' + (err.message || err));
      restoreDisplay(oldName);
    }
  }

  async function onDelete(v) {
    let usage;
    try {
      usage = await api.variables.usage(v.id);
    } catch (err) {
      alert('Verwendungs-Prüfung fehlgeschlagen: ' + (err.message || err));
      return;
    }
    const refCount = (usage?.templates?.length || 0) + (usage?.snippets?.length || 0);
    if (refCount === 0) {
      if (!confirm(`Variable {{${v.name}}} wirklich löschen?`)) return;
    } else {
      const force = await mfDeleteGuard.show({ kind: 'Variable', name: v.name, usage });
      if (!force) return;
    }
    try {
      await api.variables.delete(v.id);
      _variables = _variables.filter(x => x.id !== v.id);
      if (_activePrefix !== 'all' && !_variables.some(x => prefixOf(x.name) === _activePrefix)) {
        _activePrefix = 'all';
      }
      renderPrefixFilter();
      render();
    } catch (err) {
      alert('Löschen fehlgeschlagen: ' + (err.message || err));
    }
  }

  function onCreate() {
    const tbody = document.getElementById('variables-tbody');
    if (!tbody) return;
    if (tbody.querySelector('tr.var-draft')) return;
    const empty = document.getElementById('variables-empty');
    if (empty) empty.style.display = 'none';

    const tr = document.createElement('tr');
    tr.className = 'var-draft';
    tr.innerHTML = `
      <td><input type="text" class="var-inline-input draft-name" placeholder="variable_name"></td>
      <td><input type="text" class="var-inline-input draft-value" placeholder="Wert"></td>
      <td>—</td>
      <td class="var-actions">
        <button class="row-btn" data-action="save" title="Speichern">✓</button>
        <button class="row-btn" data-action="cancel" title="Abbrechen">✕</button>
      </td>
    `;
    tbody.insertBefore(tr, tbody.firstChild);
    const nameInput = tr.querySelector('.draft-name');
    nameInput.focus();

    async function save() {
      const name = tr.querySelector('.draft-name').value.trim().toLowerCase();
      const value = tr.querySelector('.draft-value').value;
      if (!name) { alert('Name fehlt'); return; }
      if (RESERVED_NAMES.has(name)) {
        alert(`„${name}" ist für Kontakt-Felder reserviert (wird beim Versand pro Empfänger ersetzt). Bitte anderen Namen wählen.`);
        return;
      }
      if (!NAME_RE.test(name)) {
        alert('Name ungültig — nur Kleinbuchstaben, Ziffern, Unterstriche; Start mit Buchstabe oder _');
        return;
      }
      try {
        const created = await api.variables.create({ name, value });
        _variables.push(created);
        _variables.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        renderPrefixFilter();
        render();
      } catch (err) {
        alert('Anlegen fehlgeschlagen: ' + (err.message || err));
      }
    }
    function cancel() {
      tr.remove();
      if (_variables.length === 0 && document.getElementById('variables-empty')) {
        document.getElementById('variables-empty').style.display = 'block';
      }
    }

    tr.querySelector('[data-action="save"]').addEventListener('click', save);
    tr.querySelector('[data-action="cancel"]').addEventListener('click', cancel);
    tr.querySelectorAll('input').forEach(inp => {
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); save(); }
        else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
      });
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('btn-var-new');
    if (btn) btn.addEventListener('click', onCreate);
    window.addEventListener('mf:section-changed', (e) => {
      if (e.detail.section === 'variables') load();
    });
    if (document.body.dataset.activeTab === 'templates' &&
        document.getElementById('templates-main')?.dataset.activeSection === 'variables') {
      load();
    }
  });

  window.mfVariables = { load, reload: () => load(true) };
})();
