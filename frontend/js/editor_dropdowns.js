// Wiederverwendbare Dropdown-Komponente fuer die Editor-Toolbars.
// Wird von Snippet- und Template-Editor genutzt um Variablen/Snippets
// per Klick an Cursor-Position in eine Textarea einzufuegen.

(function () {
  let _activePopup = null;

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function close() {
    if (_activePopup) {
      _activePopup.remove();
      _activePopup = null;
      document.removeEventListener('click', _outsideClick, true);
      document.removeEventListener('keydown', _keyHandler);
    }
  }

  function _outsideClick(e) {
    if (_activePopup && !_activePopup.contains(e.target)) close();
  }

  function _keyHandler(e) {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
  }

  // Inserts text at cursor of a textarea/input and dispatches 'input' event.
  function insertAtCursor(textareaId, text) {
    const ta = document.getElementById(textareaId);
    if (!ta) return;
    const s = ta.selectionStart, e = ta.selectionEnd;
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
    ta.selectionStart = ta.selectionEnd = s + text.length;
    ta.focus();
    ta.dispatchEvent(new Event('input'));
  }

  // opts:
  //   trigger:   HTMLElement (Button) – Popup wird darunter platziert
  //   loadItems: async () => Array<{ label, sublabel?, value, actions?: [{label,value,title?}] }>
  //   onSelect:  (item, actionValue?) => void
  //   emptyText?: string
  //   searchPlaceholder?: string
  async function open(opts) {
    close();

    const rect = opts.trigger.getBoundingClientRect();
    const popup = document.createElement('div');
    popup.className = 'editor-dropdown';
    popup.innerHTML = `
      <input type="text" class="ed-search" placeholder="${escapeHtml(opts.searchPlaceholder || 'Filter…')}">
      <div class="ed-list"></div>
    `;
    popup.style.left = Math.min(rect.left, window.innerWidth - 320) + 'px';
    popup.style.top = (rect.bottom + 4) + 'px';
    document.body.appendChild(popup);
    _activePopup = popup;

    const search = popup.querySelector('.ed-search');
    const list = popup.querySelector('.ed-list');

    let items = [];
    list.innerHTML = '<div class="ed-loading">Lade…</div>';
    try {
      items = await opts.loadItems();
    } catch (err) {
      list.innerHTML = `<div class="ed-error">Fehler: ${escapeHtml(err.message || String(err))}</div>`;
      return;
    }

    function render(filter = '') {
      const q = filter.trim().toLowerCase();
      const filtered = q
        ? items.filter(i => ((i.label || '') + ' ' + (i.sublabel || '')).toLowerCase().includes(q))
        : items;

      if (filtered.length === 0) {
        list.innerHTML = `<div class="ed-empty">${escapeHtml(opts.emptyText || 'Nichts gefunden')}</div>`;
        return;
      }

      list.innerHTML = '';
      filtered.forEach((item, idx) => {
        const row = document.createElement('div');
        row.className = 'ed-row';
        if (idx === 0) row.classList.add('active');

        const main = document.createElement('div');
        main.className = 'ed-row-main';
        let html = `<span class="ed-row-label">${escapeHtml(item.label)}</span>`;
        if (item.sublabel) html += `<span class="ed-row-sub">${escapeHtml(item.sublabel)}</span>`;
        main.innerHTML = html;
        row.appendChild(main);

        if (item.actions && item.actions.length) {
          const actions = document.createElement('div');
          actions.className = 'ed-row-actions';
          item.actions.forEach(a => {
            const btn = document.createElement('button');
            btn.className = 'ed-action-btn';
            btn.textContent = a.label;
            if (a.title) btn.title = a.title;
            btn.addEventListener('click', (ev) => {
              ev.stopPropagation();
              opts.onSelect(item, a.value);
              close();
            });
            actions.appendChild(btn);
          });
          row.appendChild(actions);
        } else {
          main.style.cursor = 'pointer';
          main.addEventListener('click', () => {
            opts.onSelect(item);
            close();
          });
        }

        list.appendChild(row);
      });
    }

    render();

    search.addEventListener('input', () => render(search.value));
    search.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const active = list.querySelector('.ed-row.active');
        if (!active) return;
        // Primaere Aktion: erster Action-Button oder Haupt-Main-Click
        const primary = active.querySelector('.ed-action-btn')
                       || active.querySelector('.ed-row-main');
        primary?.click();
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const rows = list.querySelectorAll('.ed-row');
        if (!rows.length) return;
        let idx = Array.from(rows).findIndex(r => r.classList.contains('active'));
        if (idx < 0) idx = 0;
        idx = e.key === 'ArrowDown' ? Math.min(idx + 1, rows.length - 1) : Math.max(idx - 1, 0);
        rows.forEach(r => r.classList.remove('active'));
        rows[idx].classList.add('active');
        rows[idx].scrollIntoView({ block: 'nearest' });
      }
    });

    setTimeout(() => search.focus(), 0);
    setTimeout(() => {
      document.addEventListener('click', _outsideClick, true);
      document.addEventListener('keydown', _keyHandler);
    }, 0);
  }

  window.mfDropdown = { open, close, insertAtCursor };
})();
