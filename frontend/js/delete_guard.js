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

  // ── Rename-Guard ───────────────────────────────────────────────
  // Wird vor dem Speichern eines umbenannten Variablen-/Snippet-Namens
  // angezeigt, wenn der alte Name noch in Templates/Snippets referenziert
  // wird. Returnt Promise<'replace' | 'keep' | 'cancel'>.

  function ensureRenameOverlay() {
    let ov = document.getElementById('rename-guard-overlay');
    if (ov) return ov;
    ov = document.createElement('div');
    ov.id = 'rename-guard-overlay';
    ov.className = 'guard-overlay';
    ov.style.display = 'none';
    ov.innerHTML = `
      <div id="rename-guard-box" class="guard-box">
        <div id="rename-guard-header" class="guard-header"><span id="rename-guard-title"></span></div>
        <div id="rename-guard-body" class="guard-body">
          <p id="rename-guard-intro"></p>
          <div id="rename-guard-usage"></div>
        </div>
        <div id="rename-guard-footer" class="guard-footer">
          <button class="action-btn" id="rename-guard-cancel">Abbrechen</button>
          <button class="action-btn" id="rename-guard-keep">Nur Namen ändern</button>
          <button class="action-btn primary" id="rename-guard-replace">Auch in Vorlagen ändern</button>
        </div>
      </div>
    `;
    document.body.appendChild(ov);
    return ov;
  }

  function showRename(opts) {
    return new Promise((resolve) => {
      const ov = ensureRenameOverlay();
      const oldRef = opts.kind === 'Variable' ? `{{${opts.oldName}}}` : `{{> ${opts.oldName}}}`;
      const newRef = opts.kind === 'Variable' ? `{{${opts.newName}}}` : `{{> ${opts.newName}}}`;
      document.getElementById('rename-guard-title').textContent =
        `${opts.kind} umbenennen: „${opts.oldName}" → „${opts.newName}"`;
      document.getElementById('rename-guard-intro').innerHTML =
        `Der alte Name <code>${escapeHtml(oldRef)}</code> wird noch verwendet. Soll er überall durch <code>${escapeHtml(newRef)}</code> ersetzt werden?`;

      const usageEl = document.getElementById('rename-guard-usage');
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
      usageEl.innerHTML = html;
      ov.style.display = 'flex';

      const cancelBtn = document.getElementById('rename-guard-cancel');
      const keepBtn = document.getElementById('rename-guard-keep');
      const replaceBtn = document.getElementById('rename-guard-replace');

      function cleanup(result) {
        cancelBtn.removeEventListener('click', onCancel);
        keepBtn.removeEventListener('click', onKeep);
        replaceBtn.removeEventListener('click', onReplace);
        ov.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey);
        ov.style.display = 'none';
        resolve(result);
      }
      function onCancel() { cleanup('cancel'); }
      function onKeep() { cleanup('keep'); }
      function onReplace() { cleanup('replace'); }
      function onBackdrop(e) { if (e.target === ov) cleanup('cancel'); }
      function onKey(e) { if (e.key === 'Escape') cleanup('cancel'); }

      cancelBtn.addEventListener('click', onCancel);
      keepBtn.addEventListener('click', onKeep);
      replaceBtn.addEventListener('click', onReplace);
      ov.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey);
    });
  }

  window.mfRenameGuard = { show: showRename };
})();
