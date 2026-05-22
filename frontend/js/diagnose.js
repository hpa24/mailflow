// Diagnose-Tab: zeigt Sync-Auffälligkeiten (Duplikat-Skips + Fetch-Fehler) aus
// dem Backend-Ringpuffer. Re-Loads bei Tab-Wechsel auf 'diagnose' und auf Klick
// des "Neu laden"-Buttons.

(function () {
  const listEl = document.getElementById('diagnose-list');
  const btnRefresh = document.getElementById('diagnose-refresh');

  if (!listEl || !btnRefresh) return;

  function _accountShort(acc) {
    return (acc || '').slice(0, 8);
  }

  function _esc(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  async function load() {
    listEl.innerHTML = '<div id="diagnose-empty">Lade …</div>';
    try {
      const data = await apiFetch('/diagnostics/sync-skips');
      const items = (data.items || []).slice().reverse();  // neueste oben
      if (items.length === 0) {
        listEl.innerHTML = '<div id="diagnose-empty">Keine Auffälligkeiten seit Backend-Start.</div>';
        return;
      }
      const rows = [
        '<div class="diagnose-row header"><div>Zeit (UTC)</div><div>Kind</div><div>Account</div><div>UID</div><div>Folder · Detail</div></div>',
      ];
      for (const e of items) {
        rows.push(
          '<div class="diagnose-row">' +
            '<div>' + _esc(e.ts) + '</div>' +
            '<div class="kind-' + _esc(e.kind) + '">' + _esc(e.kind) + '</div>' +
            '<div>' + _esc(_accountShort(e.account)) + '</div>' +
            '<div>' + _esc(e.uid) + '</div>' +
            '<div class="detail" title="' + _esc(e.detail) + '"><strong>' + _esc(e.folder) + '</strong> · ' + _esc(e.detail) + '</div>' +
          '</div>'
        );
      }
      listEl.innerHTML = rows.join('');
    } catch (exc) {
      listEl.innerHTML = '<div id="diagnose-empty">Fehler beim Laden: ' + _esc(exc?.message || exc) + '</div>';
    }
  }

  btnRefresh.addEventListener('click', load);
  window.addEventListener('mf:tab-changed', (e) => {
    if (e.detail.tab === 'diagnose') load();
  });
})();
