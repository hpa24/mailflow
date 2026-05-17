// Variablen-Tabelle im Vorlagen-Tab. Inline-Edit auf value/description per
// Doppelklick. Neue Variable als Draft-Zeile am Tabellen-Anfang.

(function () {
  let _variables = [];
  let _loaded = false;

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
      render();
    } catch (err) {
      console.error('variables load failed:', err);
      const tbody = document.getElementById('variables-tbody');
      if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="var-error">Laden fehlgeschlagen: ${escapeHtml(err.message || String(err))}</td></tr>`;
    }
  }

  function render() {
    const tbody = document.getElementById('variables-tbody');
    const empty = document.getElementById('variables-empty');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (_variables.length === 0) {
      if (empty) empty.style.display = 'block';
      return;
    }
    if (empty) empty.style.display = 'none';
    _variables.forEach(v => tbody.appendChild(renderRow(v)));
  }

  function renderRow(v) {
    const tr = document.createElement('tr');
    tr.dataset.id = v.id;
    tr.innerHTML = `
      <td class="var-name"><code>{{${escapeHtml(v.name)}}}</code></td>
      <td class="var-value" data-field="value"></td>
      <td class="var-updated">${escapeHtml(formatDate(v.updated))}</td>
      <td class="var-actions">
        <button class="row-btn" data-action="delete" title="Löschen">✕</button>
      </td>
    `;
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

    let done = false;
    async function commit() {
      if (done) return;
      done = true;
      const newValue = input.value;
      if (newValue === oldValue) {
        td.textContent = oldValue;
        return;
      }
      try {
        const updated = await api.variables.update(v.id, { [field]: newValue });
        v[field] = updated[field] != null ? updated[field] : newValue;
        v.updated = updated.updated || new Date().toISOString();
        td.textContent = v[field];
        tr.querySelector('.var-updated').textContent = formatDate(v.updated);
      } catch (err) {
        alert('Speichern fehlgeschlagen: ' + (err.message || err));
        td.textContent = oldValue;
      }
    }
    function cancel() {
      if (done) return;
      done = true;
      td.textContent = oldValue;
    }

    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
  }

  async function onDelete(v) {
    if (!confirm(`Variable {{${v.name}}} wirklich löschen?`)) return;
    try {
      await api.variables.delete(v.id);
      _variables = _variables.filter(x => x.id !== v.id);
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
      const name = tr.querySelector('.draft-name').value.trim();
      const value = tr.querySelector('.draft-value').value;
      if (!name) { alert('Name fehlt'); return; }
      try {
        const created = await api.variables.create({ name, value });
        _variables.push(created);
        _variables.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
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
