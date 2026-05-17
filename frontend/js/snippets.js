// Snippet-Editor: Liste links, Textarea + Live-Preview rechts. Save explizit
// per Button (kein Auto-Save). Dirty-State wird vor Wechsel des aktiven
// Snippets oder vor Reload erkannt.

(function () {
  let _snippets = [];
  let _selectedId = null;
  let _draft = null;       // { name, html } im Editor; null = nichts geladen
  let _loaded = false;
  let _previewTimer = null;

  // E-Mail-sicheres Tabellen-Skelett als Startpunkt fuer neue Snippets.
  // Outlook-kompatibel: explizite Attribute statt CSS, Inline-Styles statt <style>.
  const DEFAULT_SNIPPET_HTML = `<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="border-collapse:collapse;">
  <tr>
    <td style="padding:16px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; font-size:16px; line-height:1.5; color:#1c1c1e;">

    </td>
  </tr>
</table>`;

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function isDirty() {
    if (!_draft) return false;
    if (_selectedId === null) {
      // Neues Snippet: dirty wenn Name gesetzt oder HTML vom Default abweicht
      return (_draft.name && _draft.name.length > 0) || (_draft.html !== DEFAULT_SNIPPET_HTML);
    }
    const orig = _snippets.find(s => s.id === _selectedId);
    if (!orig) return true;
    return (_draft.name !== orig.name) || (_draft.html !== orig.html);
  }

  function maybeConfirmDiscard() {
    return !isDirty() || confirm('Ungespeicherte Änderungen verwerfen?');
  }

  async function load(force = false) {
    if (_loaded && !force) return;
    try {
      _snippets = await api.snippets.list();
      _snippets.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
      _loaded = true;
      renderList();
      if (_selectedId) {
        const cur = _snippets.find(s => s.id === _selectedId);
        if (cur) loadEditor(cur);
        else clearEditor();
      }
    } catch (err) {
      console.error('snippets load failed:', err);
      const list = document.getElementById('snippets-list-items');
      if (list) list.innerHTML = `<div class="snippets-error">Laden fehlgeschlagen: ${escapeHtml(err.message || String(err))}</div>`;
    }
  }

  function renderList() {
    const list = document.getElementById('snippets-list-items');
    const empty = document.getElementById('snippets-list-empty');
    if (!list) return;
    const searchInput = document.getElementById('snippets-search');
    const q = (searchInput?.value || '').trim().toLowerCase();
    const filtered = q ? _snippets.filter(s => (s.name || '').toLowerCase().includes(q)) : _snippets;

    list.innerHTML = '';
    if (filtered.length === 0) {
      if (empty) {
        empty.textContent = _snippets.length === 0
          ? 'Noch keine Snippets. „+ Neu" anklicken.'
          : 'Kein Treffer.';
        empty.style.display = 'block';
      }
      return;
    }
    if (empty) empty.style.display = 'none';

    filtered.forEach(s => {
      const item = document.createElement('button');
      item.className = 'snippets-list-item';
      item.classList.toggle('active', s.id === _selectedId);
      item.dataset.id = s.id;
      item.textContent = s.name;
      item.addEventListener('click', () => onSelect(s));
      list.appendChild(item);
    });
  }

  function onSelect(s) {
    if (_selectedId === s.id && !isDirty()) return;
    if (!maybeConfirmDiscard()) return;
    loadEditor(s);
  }

  function loadEditor(s) {
    _selectedId = s.id;
    _draft = { name: s.name, html: s.html || '' };
    showEditor();
    document.getElementById('snippet-name-input').value = s.name;
    document.getElementById('snippet-html-textarea').value = s.html || '';
    document.getElementById('snippet-ref').textContent = `{{> ${s.name}}}`;
    document.getElementById('snippet-delete-btn').disabled = false;
    updatePreview();
    renderList();
    updateDirtyIndicator();
  }

  function clearEditor() {
    _selectedId = null;
    _draft = null;
    showPlaceholder();
    renderList();
  }

  function showEditor() {
    document.getElementById('snippets-editor-empty').style.display = 'none';
    document.getElementById('snippets-editor-pane').style.display = 'grid';
  }
  function showPlaceholder() {
    document.getElementById('snippets-editor-empty').style.display = 'flex';
    document.getElementById('snippets-editor-pane').style.display = 'none';
  }

  function onNew() {
    if (!maybeConfirmDiscard()) return;
    _selectedId = null;
    _draft = { name: '', html: DEFAULT_SNIPPET_HTML };
    showEditor();
    document.getElementById('snippet-name-input').value = '';
    document.getElementById('snippet-html-textarea').value = DEFAULT_SNIPPET_HTML;
    document.getElementById('snippet-ref').textContent = '{{> name_kommt_beim_speichern}}';
    document.getElementById('snippet-delete-btn').disabled = true;
    updatePreview();
    renderList();
    updateDirtyIndicator();
    document.getElementById('snippet-name-input').focus();
  }

  function updatePreview() {
    const iframe = document.getElementById('snippet-preview-iframe');
    if (!iframe || !_draft) return;
    clearTimeout(_previewTimer);
    _previewTimer = setTimeout(() => {
      // Preview rendert das rohe Snippet-HTML in ein neutrales Wrapper-Dokument
      iframe.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;padding:16px;font-family:-apple-system,sans-serif;color:#1c1c1e;background:#fff;}</style></head><body>${_draft.html || '<em style="color:#aaa">Leeres Snippet</em>'}</body></html>`;
    }, 200);
  }

  function updateDirtyIndicator() {
    const btn = document.getElementById('snippet-save-btn');
    if (!btn) return;
    if (isDirty()) {
      btn.classList.add('dirty');
      btn.textContent = 'Speichern *';
    } else {
      btn.classList.remove('dirty');
      btn.textContent = 'Speichern';
    }
  }

  async function onSave() {
    if (!_draft) return;
    const name = (_draft.name || '').trim().toLowerCase();
    if (!/^[a-z_][a-z0-9_]{0,49}$/.test(name)) {
      alert('Name ungültig. Erlaubt: 1–50 Zeichen, a–z, 0–9, _; Start mit Buchstabe oder _.');
      return;
    }
    try {
      if (_selectedId) {
        const updated = await api.snippets.update(_selectedId, { name, html: _draft.html });
        Object.assign(_snippets.find(s => s.id === _selectedId), updated);
      } else {
        const created = await api.snippets.create({ name, html: _draft.html });
        _snippets.push(created);
        _selectedId = created.id;
      }
      _snippets.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
      _draft = { name, html: _draft.html };
      document.getElementById('snippet-ref').textContent = `{{> ${name}}}`;
      document.getElementById('snippet-delete-btn').disabled = false;
      renderList();
      updateDirtyIndicator();
    } catch (err) {
      alert('Speichern fehlgeschlagen: ' + (err.message || err));
    }
  }

  async function onDelete() {
    if (!_selectedId) return;
    const cur = _snippets.find(s => s.id === _selectedId);
    if (!cur) return;
    if (!confirm(`Snippet "${cur.name}" wirklich löschen?`)) return;
    try {
      await api.snippets.delete(_selectedId);
      _snippets = _snippets.filter(s => s.id !== _selectedId);
      clearEditor();
    } catch (err) {
      alert('Löschen fehlgeschlagen: ' + (err.message || err));
    }
  }

  function bindEditor() {
    const nameIn = document.getElementById('snippet-name-input');
    const htmlIn = document.getElementById('snippet-html-textarea');
    if (nameIn) nameIn.addEventListener('input', () => {
      if (!_draft) return;
      _draft.name = nameIn.value;
      updateDirtyIndicator();
    });
    if (htmlIn) htmlIn.addEventListener('input', () => {
      if (!_draft) return;
      _draft.html = htmlIn.value;
      updatePreview();
      updateDirtyIndicator();
    });
    // Tab-Taste in der Textarea → 2 Leerzeichen statt Fokus-Wechsel
    if (htmlIn) htmlIn.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') {
        e.preventDefault();
        const s = htmlIn.selectionStart;
        const eEnd = htmlIn.selectionEnd;
        htmlIn.value = htmlIn.value.slice(0, s) + '  ' + htmlIn.value.slice(eEnd);
        htmlIn.selectionStart = htmlIn.selectionEnd = s + 2;
        htmlIn.dispatchEvent(new Event('input'));
      }
    });
  }

  async function copyToClipboard(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      if (btn) {
        const oldLabel = btn.textContent;
        btn.textContent = '✓ Kopiert';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = oldLabel;
          btn.classList.remove('copied');
        }, 1200);
      }
    } catch (err) {
      alert('Kopieren fehlgeschlagen: ' + (err.message || err));
    }
  }

  function onCopyRef(e) {
    if (!_draft) return;
    const name = (_draft.name || '').trim().toLowerCase();
    if (!name) { alert('Snippet muss erst gespeichert sein.'); return; }
    copyToClipboard(`{{> ${name}}}`, e.currentTarget);
  }

  function onCopyHtml(e) {
    if (!_draft) return;
    copyToClipboard(_draft.html || '', e.currentTarget);
  }

  function bindGlobal() {
    document.getElementById('btn-snippet-new')?.addEventListener('click', onNew);
    document.getElementById('snippet-save-btn')?.addEventListener('click', onSave);
    document.getElementById('snippet-delete-btn')?.addEventListener('click', onDelete);
    document.getElementById('snippet-copy-ref')?.addEventListener('click', onCopyRef);
    document.getElementById('snippet-copy-html')?.addEventListener('click', onCopyHtml);
    document.getElementById('snippets-search')?.addEventListener('input', renderList);

    window.addEventListener('mf:tab-changed', (e) => {
      if (e.detail.tab === 'templates') load();
    });
    window.addEventListener('mf:section-changed', (e) => {
      if (e.detail.section === 'snippets') load();
    });

    // Warnen vor Reload bei Dirty-State
    window.addEventListener('beforeunload', (e) => {
      if (isDirty()) {
        e.preventDefault();
        e.returnValue = '';
      }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    bindEditor();
    bindGlobal();
    showPlaceholder();
    if (document.body.dataset.activeTab === 'templates' &&
        document.getElementById('templates-main')?.dataset.activeSection === 'snippets') {
      load();
    }
  });

  window.mfSnippets = { load, reload: () => load(true) };
})();
