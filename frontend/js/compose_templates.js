// "Aus Vorlage" im Compose-Editor:
// - oeffnet Modal mit Template-Liste (Praefix-Filter + Suche)
// - Auswahl ruft /templates/render und schreibt Subject + HTML in den Editor
// - Banner unter Subject zeigt Vorlagenname + unaufgeloeste Platzhalter

(function () {
  let _templates = [];
  let _activePrefix = 'all';

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ─ Modal ─────────────────────────────────────────────────────
  function openModal() {
    document.getElementById('template-picker-overlay').style.display = 'flex';
    document.getElementById('tplp-search').value = '';
    loadList();
    setTimeout(() => document.getElementById('tplp-search')?.focus(), 50);
  }

  function closeModal() {
    document.getElementById('template-picker-overlay').style.display = 'none';
  }

  async function loadList() {
    const list = document.getElementById('tplp-list');
    list.innerHTML = '<div class="tplp-loading">Lade…</div>';
    try {
      _templates = await api.templates.list();
      _templates.sort((a, b) => {
        const p = (a.prefix || '').localeCompare(b.prefix || '');
        return p !== 0 ? p : (a.name || '').localeCompare(b.name || '');
      });
      renderPrefixFilter();
      renderList();
    } catch (err) {
      list.innerHTML = `<div class="tplp-error">Fehler: ${escapeHtml(err.message || String(err))}</div>`;
    }
  }

  function renderPrefixFilter() {
    const bar = document.getElementById('tplp-prefix-filter');
    if (!bar) return;
    const prefixes = Array.from(new Set(
      _templates.map(t => t.prefix).filter(Boolean)
    )).sort();
    bar.innerHTML = '';
    if (prefixes.length === 0) { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    const mk = (label, value) => {
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
    bar.appendChild(mk('Alle', 'all'));
    prefixes.forEach(p => bar.appendChild(mk(p, p)));
  }

  function renderList() {
    const list = document.getElementById('tplp-list');
    const empty = document.getElementById('tplp-empty');
    const q = (document.getElementById('tplp-search')?.value || '').trim().toLowerCase();
    const items = _templates.filter(t => {
      if (_activePrefix !== 'all' && (t.prefix || '') !== _activePrefix) return false;
      if (q) {
        const hay = `${t.prefix || ''} ${t.name || ''} ${t.subject || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    if (items.length === 0) {
      list.innerHTML = '';
      empty.style.display = 'block';
      empty.textContent = _templates.length === 0 ? 'Keine Vorlagen angelegt.' : 'Kein Treffer.';
      return;
    }
    empty.style.display = 'none';

    list.innerHTML = '';
    let lastPrefix = null;
    items.forEach(t => {
      const p = t.prefix || '(ohne Präfix)';
      if (p !== lastPrefix) {
        const h = document.createElement('div');
        h.className = 'tplp-group';
        h.textContent = p;
        list.appendChild(h);
        lastPrefix = p;
      }
      const row = document.createElement('button');
      row.className = 'tplp-item';
      row.innerHTML = `<span class="tplp-item-name">${escapeHtml(t.name)}</span><span class="tplp-item-sub">${escapeHtml(t.subject || '')}</span>`;
      row.addEventListener('click', () => onPick(t));
      list.appendChild(row);
    });
  }

  // ─ Pick + Load ───────────────────────────────────────────────
  async function onPick(t) {
    const body = document.getElementById('ci-body');
    const subj = document.getElementById('ci-subject');
    const bodyEmpty = !body || body.innerHTML.replace(/<br\s*\/?>/g, '').trim() === '';
    const subjEmpty = !subj || !subj.value.trim();
    if (!(bodyEmpty && subjEmpty)) {
      if (!confirm('Vorhandenen Inhalt überschreiben?')) return;
    }
    closeModal();

    let result;
    try {
      result = await api.templates.render({
        html: t.html_body || '',
        subject: t.subject || '',
      });
    } catch (err) {
      alert('Rendern fehlgeschlagen: ' + (err.message || err));
      return;
    }

    if (subj) subj.value = result.subject || '';
    if (body) body.innerHTML = result.html || '';

    showBanner(t, result.unresolved || []);
  }

  // ─ Banner ────────────────────────────────────────────────────
  function showBanner(t, unresolved) {
    const banner = document.getElementById('ci-template-banner');
    if (!banner) return;
    const nameEl = document.getElementById('ci-tmpl-name');
    const unresolvedEl = document.getElementById('ci-tmpl-unresolved');
    nameEl.textContent = `${t.prefix ? t.prefix + '/' : ''}${t.name}`;
    if (unresolved && unresolved.length) {
      const names = Array.from(new Set(unresolved.map(u => u.placeholder)));
      unresolvedEl.textContent = ` · Offene Platzhalter: ${names.join(', ')}`;
    } else {
      unresolvedEl.textContent = '';
    }
    banner.style.display = 'flex';
  }

  function clearBanner() {
    const banner = document.getElementById('ci-template-banner');
    if (banner) banner.style.display = 'none';
  }

  // ─ Wiring ────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('btn-from-template')?.addEventListener('click', openModal);
    document.getElementById('tplp-close')?.addEventListener('click', closeModal);
    document.getElementById('ci-tmpl-clear')?.addEventListener('click', clearBanner);
    document.getElementById('tplp-search')?.addEventListener('input', renderList);
    document.getElementById('template-picker-overlay')?.addEventListener('click', (e) => {
      if (e.target.id === 'template-picker-overlay') closeModal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' &&
          document.getElementById('template-picker-overlay')?.style.display === 'flex') {
        closeModal();
      }
    });
    // Compose-Cancel raeumt den Banner mit auf
    document.getElementById('btn-compose-cancel')?.addEventListener('click', clearBanner);
  });

  window.mfComposeTemplates = { openModal, clearBanner };
})();
