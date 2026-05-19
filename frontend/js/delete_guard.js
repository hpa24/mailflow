// Loesch-Schutz fuer Variablen + Snippets: zeigt einen Modal-Dialog mit
// Treffer-Liste, wenn das Objekt noch referenziert ist. Bietet 'Trotzdem
// loeschen' an. Nutzt einen einmalig angehaengten Overlay-Container.

(function () {
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function ensureOverlay() {
    let ov = document.getElementById('delete-guard-overlay');
    if (ov) return ov;
    ov = document.createElement('div');
    ov.id = 'delete-guard-overlay';
    ov.style.display = 'none';
    ov.innerHTML = `
      <div id="delete-guard-box">
        <div id="delete-guard-header"><span id="delete-guard-title"></span></div>
        <div id="delete-guard-body">
          <p id="delete-guard-intro"></p>
          <div id="delete-guard-usage"></div>
        </div>
        <div id="delete-guard-footer">
          <button class="action-btn" id="delete-guard-cancel">Abbrechen</button>
          <button class="action-btn danger" id="delete-guard-force">Trotzdem löschen</button>
        </div>
      </div>
    `;
    document.body.appendChild(ov);
    return ov;
  }

  function close() {
    const ov = document.getElementById('delete-guard-overlay');
    if (ov) ov.style.display = 'none';
  }

  /**
   * Zeigt den Modal. Returnt Promise<boolean> — true = User hat 'Trotzdem
   * loeschen' bestaetigt, false = abgebrochen.
   *
   * @param {object} opts
   * @param {string} opts.kind        'Variable' | 'Snippet'
   * @param {string} opts.name        Anzeige-Name (z.B. 'kurs_termin')
   * @param {object} opts.usage       {templates: [{prefix,name,fields[]}], snippets: [{name}]}
   */
  function show(opts) {
    return new Promise((resolve) => {
      const ov = ensureOverlay();
      const refLabel = opts.kind === 'Variable' ? `{{${opts.name}}}` : `{{> ${opts.name}}}`;
      document.getElementById('delete-guard-title').textContent =
        `${opts.kind} „${opts.name}" wird noch verwendet`;
      document.getElementById('delete-guard-intro').innerHTML =
        `Vor dem Löschen prüfen, wo <code>${escapeHtml(refLabel)}</code> noch referenziert wird. Nach dem Löschen bleiben diese Referenzen als unaufgelöste Platzhalter in den Mails.`;

      const usageEl = document.getElementById('delete-guard-usage');
      const tpls = opts.usage?.templates || [];
      const snips = opts.usage?.snippets || [];
      let html = '';
      if (tpls.length > 0) {
        html += `<div class="dg-section"><h4>In Vorlagen (${tpls.length})</h4><ul>`;
        tpls.forEach(t => {
          const where = (t.fields || []).join(', ');
          const prefix = t.prefix ? `<span class="dg-prefix">${escapeHtml(t.prefix)}</span> ` : '';
          html += `<li>${prefix}<strong>${escapeHtml(t.name)}</strong>${where ? ` <span class="dg-where">(${escapeHtml(where)})</span>` : ''}</li>`;
        });
        html += '</ul></div>';
      }
      if (snips.length > 0) {
        html += `<div class="dg-section"><h4>In Snippets (${snips.length})</h4><ul>`;
        snips.forEach(s => {
          html += `<li><strong>${escapeHtml(s.name)}</strong></li>`;
        });
        html += '</ul></div>';
      }
      if (tpls.length === 0 && snips.length === 0) {
        html = '<div class="dg-section"><em>Keine Referenzen gefunden.</em></div>';
      }
      usageEl.innerHTML = html;
      ov.style.display = 'flex';

      const cancelBtn = document.getElementById('delete-guard-cancel');
      const forceBtn = document.getElementById('delete-guard-force');

      function cleanup(result) {
        cancelBtn.removeEventListener('click', onCancel);
        forceBtn.removeEventListener('click', onForce);
        ov.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey);
        close();
        resolve(result);
      }
      function onCancel() { cleanup(false); }
      function onForce()  { cleanup(true); }
      function onBackdrop(e) { if (e.target === ov) cleanup(false); }
      function onKey(e)   { if (e.key === 'Escape') cleanup(false); }

      cancelBtn.addEventListener('click', onCancel);
      forceBtn.addEventListener('click', onForce);
      ov.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey);
    });
  }

  window.mfDeleteGuard = { show };
})();
