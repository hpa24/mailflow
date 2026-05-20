// Phase 3b — Subview "Bouncte Kontakte" im Vorlagen-Tab.
// Lädt alle contacts mit bounced=true und erlaubt Reset per Klick.

(function () {
  let _loaded = false;
  let _items = [];

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function formatDate(iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleString('de-DE', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    } catch (_) { return iso; }
  }

  async function load() {
    const tbody = document.getElementById('bounced-tbody');
    const emptyEl = document.getElementById('bounced-empty');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="groups-loading">Lade…</td></tr>';
    try {
      _items = await api.contacts.bounced();
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="groups-error">Fehler beim Laden: ${escapeHtml(err.message || err)}</td></tr>`;
      return;
    }
    render();
    if (emptyEl) emptyEl.style.display = _items.length === 0 ? 'block' : 'none';
    _loaded = true;
  }

  function render() {
    const tbody = document.getElementById('bounced-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    _items.forEach(c => {
      const tr = document.createElement('tr');
      tr.dataset.id = c.id;
      tr.innerHTML = `
        <td>${escapeHtml(c.email)}</td>
        <td>${escapeHtml(c.name || '')}</td>
        <td>${escapeHtml(formatDate(c.bounced_at))}</td>
        <td class="bounced-reason" title="${escapeHtml(c.bounced_reason || '')}">${escapeHtml((c.bounced_reason || '').slice(0, 100))}</td>
        <td><button class="row-btn" data-action="clear" title="Bounce zurücksetzen">↺ Reset</button></td>
      `;
      tr.querySelector('[data-action="clear"]').addEventListener('click', () => clearOne(c));
      tbody.appendChild(tr);
    });
  }

  async function clearOne(c) {
    if (!confirm(`Bounce-Flag für „${c.email}" zurücksetzen?\nDie Adresse wird beim nächsten Massenversand wieder beliefert.`)) return;
    try {
      await api.contacts.clearBounce(c.id);
      _items = _items.filter(x => x.id !== c.id);
      render();
      const emptyEl = document.getElementById('bounced-empty');
      if (emptyEl) emptyEl.style.display = _items.length === 0 ? 'block' : 'none';
    } catch (err) {
      alert('Reset fehlgeschlagen: ' + (err.message || err));
    }
  }

  window.addEventListener('mf:section-changed', (e) => {
    if (e.detail && e.detail.section === 'bounced') {
      load();  // immer neu laden — Status kann sich zwischen Switchen ändern
    }
  });

  // Bei initialem Page-Load mit gespeicherter section=bounced auch laden.
  document.addEventListener('DOMContentLoaded', () => {
    const main = document.getElementById('templates-main');
    if (main && main.dataset.activeSection === 'bounced') load();
  });

  window.mfBouncedContacts = { reload: load };
})();
