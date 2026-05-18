// Templates-Editor: Liste links (gruppiert nach Praefix, mit Filter und Suche),
// Editor rechts mit Praefix/Name/Subject + Textarea + Live-Preview + Erkannt-Box.
// Save explizit per Button. Dirty-State markiert ungespeicherte Aenderungen.

(function () {
  let _templates = [];
  let _selectedId = null;
  let _draft = null;
  let _loaded = false;
  let _previewTimer = null;
  let _activePrefix = 'all';

  // Empfaengt-sicheres Tabellen-Skelett als Startpunkt fuer neue Vorlagen.
  const DEFAULT_TEMPLATE_HTML = `
<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="border-collapse:collapse; background:#f5f5f7;">
<tr>
<td align="left" style="padding:0px;">

<table width="600" cellpadding="0" cellspacing="0" border="0" role="presentation" style="border-collapse:collapse; background:#ffffff; max-width:600px; width:100%;">
<tr>
<td style="padding:24px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; font-size:16px; line-height:1.2; color:#1c1c1e;">

            <h1 style="margin:0 0 16px 0; font-size:24px; font-weight:700; color:#005a93;">
              Hallo {{name}},
            </h1>
            <p style="margin:0 0 16px 0;">
              hier kommt dein Vorlagen-Text. Variablen wie {{kurs_termin}} oder Snippet-Referenzen wie {{> footer}} werden beim Versand ersetzt.
            </p>
            <p style="margin:0;">
              Viele Gruesse
            </p>

</td>
</tr>
</table>

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
      return (_draft.prefix && _draft.prefix.length > 0)
          || (_draft.name && _draft.name.length > 0)
          || (_draft.subject && _draft.subject.length > 0)
          || (_draft.html_body !== DEFAULT_TEMPLATE_HTML);
    }
    const orig = _templates.find(t => t.id === _selectedId);
    if (!orig) return true;
    return _draft.prefix !== (orig.prefix || '')
        || _draft.name !== orig.name
        || _draft.subject !== (orig.subject || '')
        || _draft.html_body !== (orig.html_body || '');
  }

  function maybeConfirmDiscard() {
    return !isDirty() || confirm('Ungespeicherte Änderungen verwerfen?');
  }

  async function load(force = false) {
    if (_loaded && !force) return;
    try {
      _templates = await api.templates.list();
      _templates.sort(sortFn);
      _loaded = true;
      renderPrefixFilter();
      renderList();
      if (_selectedId) {
        const cur = _templates.find(t => t.id === _selectedId);
        if (cur) loadEditor(cur);
        else clearEditor();
      }
    } catch (err) {
      console.error('templates load failed:', err);
      const list = document.getElementById('templates-list-items');
      if (list) list.innerHTML = `<div class="templates-error">Laden fehlgeschlagen: ${escapeHtml(err.message || String(err))}</div>`;
    }
  }

  function sortFn(a, b) {
    const pa = (a.prefix || '').localeCompare(b.prefix || '');
    if (pa !== 0) return pa;
    return (a.name || '').localeCompare(b.name || '');
  }

  function renderPrefixFilter() {
    const bar = document.getElementById('templates-prefix-filter');
    if (!bar) return;
    const prefixes = Array.from(new Set(
      _templates.map(t => t.prefix).filter(Boolean)
    )).sort();
    bar.innerHTML = '';
    const mkBtn = (label, value) => {
      const btn = document.createElement('button');
      btn.className = 'tpl-prefix-btn';
      btn.classList.toggle('active', _activePrefix === value);
      btn.textContent = label;
      btn.addEventListener('click', () => {
        _activePrefix = value;
        renderPrefixFilter();
        renderList();
      });
      return btn;
    };
    bar.appendChild(mkBtn('Alle', 'all'));
    prefixes.forEach(p => bar.appendChild(mkBtn(p, p)));
    if (prefixes.length === 0) bar.style.display = 'none';
    else bar.style.display = 'flex';
  }

  function visible() {
    const q = (document.getElementById('templates-search')?.value || '').trim().toLowerCase();
    return _templates.filter(t => {
      if (_activePrefix !== 'all' && (t.prefix || '') !== _activePrefix) return false;
      if (q) {
        const hay = `${t.prefix || ''} ${t.name || ''} ${t.subject || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  function renderList() {
    const list = document.getElementById('templates-list-items');
    const empty = document.getElementById('templates-list-empty');
    if (!list) return;
    list.innerHTML = '';
    const items = visible();
    if (items.length === 0) {
      if (empty) {
        empty.textContent = _templates.length === 0
          ? 'Noch keine Vorlagen. „+ Neu" anklicken.'
          : 'Kein Treffer.';
        empty.style.display = 'block';
      }
      return;
    }
    if (empty) empty.style.display = 'none';

    // Gruppiert nach Praefix
    let lastPrefix = null;
    items.forEach(t => {
      const p = t.prefix || '(ohne Präfix)';
      if (p !== lastPrefix) {
        const h = document.createElement('div');
        h.className = 'templates-list-group';
        h.textContent = p;
        list.appendChild(h);
        lastPrefix = p;
      }
      const item = document.createElement('button');
      item.className = 'templates-list-item';
      item.classList.toggle('active', t.id === _selectedId);
      item.dataset.id = t.id;
      item.textContent = t.name;
      item.addEventListener('click', () => onSelect(t));
      list.appendChild(item);
    });
  }

  function onSelect(t) {
    if (_selectedId === t.id && !isDirty()) return;
    if (!maybeConfirmDiscard()) return;
    loadEditor(t);
  }

  function loadEditor(t) {
    _selectedId = t.id;
    _draft = {
      prefix: t.prefix || '',
      name: t.name,
      subject: t.subject || '',
      html_body: t.html_body || '',
    };
    showEditor();
    document.getElementById('tpl-prefix-input').value = _draft.prefix;
    document.getElementById('tpl-name-input').value = _draft.name;
    document.getElementById('tpl-subject-input').value = _draft.subject;
    document.getElementById('tpl-html-textarea').value = _draft.html_body;
    document.getElementById('tpl-delete-btn').disabled = false;
    updatePreview();
    updateDetectedBox();
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
    document.getElementById('templates-editor-empty').style.display = 'none';
    document.getElementById('templates-editor-pane').style.display = 'grid';
  }
  function showPlaceholder() {
    document.getElementById('templates-editor-empty').style.display = 'flex';
    document.getElementById('templates-editor-pane').style.display = 'none';
  }

  function onNew() {
    if (!maybeConfirmDiscard()) return;
    _selectedId = null;
    _draft = {
      prefix: _activePrefix !== 'all' ? _activePrefix : '',
      name: '',
      subject: '',
      html_body: DEFAULT_TEMPLATE_HTML,
    };
    showEditor();
    document.getElementById('tpl-prefix-input').value = _draft.prefix;
    document.getElementById('tpl-name-input').value = '';
    document.getElementById('tpl-subject-input').value = '';
    document.getElementById('tpl-html-textarea').value = DEFAULT_TEMPLATE_HTML;
    document.getElementById('tpl-delete-btn').disabled = true;
    updatePreview();
    updateDetectedBox();
    renderList();
    updateDirtyIndicator();
    const nameIn = document.getElementById('tpl-name-input');
    nameIn.focus();
  }

  function updatePreview() {
    const iframe = document.getElementById('tpl-preview-iframe');
    if (!iframe || !_draft) return;
    clearTimeout(_previewTimer);
    const captured = _draft.html_body || '';
    _previewTimer = setTimeout(async () => {
      let rendered;
      try {
        const result = await api.templates.render({ html: captured });
        rendered = result.html;
      } catch (err) {
        rendered = captured;
        console.warn('Template-Preview-Render fehlgeschlagen:', err);
      }
      iframe.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;font-family:-apple-system,sans-serif;}</style></head><body>${rendered || '<em style="color:#aaa;padding:16px;display:block">Leere Vorlage</em>'}</body></html>`;
    }, 300);
  }

  // Extrahiert Vars, Snippet-Refs und Section-IDs aus dem aktuellen HTML.
  function extractDetected(html) {
    const vars = new Set();
    const snippets = new Set();
    const sections = new Set();
    if (!html) return { vars: [], snippets: [], sections: [] };
    // {{> name}} und {{name}}
    const placeholderRe = /\{\{\s*(>?\s*)([\w.]+)\s*\}\}/g;
    let m;
    while ((m = placeholderRe.exec(html)) !== null) {
      if (m[1].includes('>')) snippets.add(m[2]);
      else vars.add(m[2]);
    }
    // <!-- @section ID -->
    const sectionRe = /<!--\s*@section\s+([\w.-]+)/g;
    while ((m = sectionRe.exec(html)) !== null) {
      sections.add(m[1]);
    }
    return {
      vars: Array.from(vars).sort(),
      snippets: Array.from(snippets).sort(),
      sections: Array.from(sections).sort(),
    };
  }

  function updateDetectedBox() {
    const box = document.getElementById('tpl-detected');
    if (!box || !_draft) return;
    const d = extractDetected(_draft.html_body);
    const pill = (txt, cls) => `<span class="tpl-pill ${cls}">${escapeHtml(txt)}</span>`;
    let parts = [];
    if (d.vars.length) parts.push(`<div><strong>Variablen:</strong> ${d.vars.map(v => pill('{{' + v + '}}', 'var')).join(' ')}</div>`);
    if (d.snippets.length) parts.push(`<div><strong>Snippets:</strong> ${d.snippets.map(s => pill('{{> ' + s + '}}', 'snippet')).join(' ')}</div>`);
    if (d.sections.length) parts.push(`<div><strong>Sections:</strong> ${d.sections.map(s => pill(s, 'section')).join(' ')}</div>`);
    box.innerHTML = parts.length ? parts.join('') : '<span class="tpl-detected-empty">Keine Variablen, Snippets oder Sections erkannt.</span>';
  }

  function updateDirtyIndicator() {
    const btn = document.getElementById('tpl-save-btn');
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
    const prefix = (_draft.prefix || '').trim().toLowerCase();
    const name = (_draft.name || '').trim();
    if (prefix && !/^[a-z0-9_]{0,30}$/.test(prefix)) {
      alert('Präfix ungültig: max 30 Zeichen, nur a–z, 0–9, _.');
      return;
    }
    if (!name) { alert('Name fehlt'); return; }
    if (name.length > 100) { alert('Name darf max 100 Zeichen lang sein'); return; }
    const payload = {
      prefix,
      name,
      subject: _draft.subject || '',
      html_body: _draft.html_body || '',
    };
    try {
      let result;
      if (_selectedId) {
        result = await api.templates.update(_selectedId, payload);
        const idx = _templates.findIndex(t => t.id === _selectedId);
        if (idx >= 0) _templates[idx] = result;
      } else {
        result = await api.templates.create(payload);
        _templates.push(result);
        _selectedId = result.id;
      }
      _templates.sort(sortFn);
      _draft = { prefix, name, subject: payload.subject, html_body: payload.html_body };
      document.getElementById('tpl-delete-btn').disabled = false;
      renderPrefixFilter();
      renderList();
      updateDirtyIndicator();
    } catch (err) {
      alert('Speichern fehlgeschlagen: ' + (err.message || err));
    }
  }

  async function onDelete() {
    if (!_selectedId) return;
    const cur = _templates.find(t => t.id === _selectedId);
    if (!cur) return;
    if (!confirm(`Vorlage "${cur.prefix ? cur.prefix + '/' : ''}${cur.name}" wirklich löschen?`)) return;
    try {
      await api.templates.delete(_selectedId);
      _templates = _templates.filter(t => t.id !== _selectedId);
      clearEditor();
      renderPrefixFilter();
    } catch (err) {
      alert('Löschen fehlgeschlagen: ' + (err.message || err));
    }
  }

  function bindEditor() {
    const bind = (id, field, withPreview = false) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('input', () => {
        if (!_draft) return;
        _draft[field] = el.value;
        if (withPreview) {
          updatePreview();
          updateDetectedBox();
        }
        updateDirtyIndicator();
      });
    };
    bind('tpl-prefix-input', 'prefix');
    bind('tpl-name-input', 'name');
    bind('tpl-subject-input', 'subject');
    bind('tpl-html-textarea', 'html_body', true);

    // Tab in der Textarea → 2 Spaces
    const ta = document.getElementById('tpl-html-textarea');
    if (ta) ta.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') {
        e.preventDefault();
        const s = ta.selectionStart, eEnd = ta.selectionEnd;
        ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(eEnd);
        ta.selectionStart = ta.selectionEnd = s + 2;
        ta.dispatchEvent(new Event('input'));
      }
    });
  }

  function onInsertVariable(e) {
    if (!_draft) return;
    mfDropdown.open({
      trigger: e.currentTarget,
      searchPlaceholder: 'Variable suchen…',
      emptyText: 'Noch keine Variablen angelegt.',
      loadItems: async () => {
        const list = await api.variables.list();
        list.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        return list.map(v => ({
          label: `{{${v.name}}}`,
          sublabel: v.value ? (v.value.length > 60 ? v.value.slice(0, 60) + '…' : v.value) : '',
          value: v.name,
        }));
      },
      onSelect: (item) => {
        mfDropdown.insertAtCursor('tpl-html-textarea', `{{${item.value}}}`);
      },
    });
  }

  function onInsertSnippet(e) {
    if (!_draft) return;
    mfDropdown.open({
      trigger: e.currentTarget,
      searchPlaceholder: 'Snippet suchen…',
      emptyText: 'Noch keine Snippets angelegt.',
      loadItems: async () => {
        const list = await api.snippets.list();
        list.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        return list.map(s => ({
          label: s.name,
          sublabel: s.html ? (s.html.replace(/\s+/g, ' ').slice(0, 60) + (s.html.length > 60 ? '…' : '')) : '',
          value: s,
          actions: [
            { label: 'Referenz', value: 'ref', title: 'Als {{> name}} einfügen (dynamisch, ändert sich mit Snippet)' },
            { label: 'Code', value: 'code', title: 'HTML-Inhalt inline kopieren (statisch, unabhängig vom Snippet)' },
          ],
        }));
      },
      onSelect: (item, actionValue) => {
        if (actionValue === 'ref') {
          mfDropdown.insertAtCursor('tpl-html-textarea', `{{> ${item.value.name}}}`);
        } else if (actionValue === 'code') {
          mfDropdown.insertAtCursor('tpl-html-textarea', item.value.html || '');
        }
      },
    });
  }

  function bindGlobal() {
    document.getElementById('btn-tpl-new')?.addEventListener('click', onNew);
    document.getElementById('tpl-save-btn')?.addEventListener('click', onSave);
    document.getElementById('tpl-delete-btn')?.addEventListener('click', onDelete);
    document.getElementById('tpl-insert-var')?.addEventListener('click', onInsertVariable);
    document.getElementById('tpl-insert-snippet')?.addEventListener('click', onInsertSnippet);
    document.getElementById('templates-search')?.addEventListener('input', renderList);

    window.addEventListener('mf:section-changed', (e) => {
      if (e.detail.section === 'templates') load();
    });

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
        document.getElementById('templates-main')?.dataset.activeSection === 'templates') {
      load();
    }
  });

  window.mfTemplates = { load, reload: () => load(true) };
})();
