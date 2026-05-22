// Compose-Modul — inline E-Mail-Editor: Toolbar, Draft-Auto-Save, Senden,
// Send-Notifications (Phase A von C4 Phase 2).
//
// Phase B (bulk-Versand) und Phase C (Attachments + Chip-Felder) liegen
// noch in inbox.js und werden in Folge-Commits hierhin gezogen.
//
// Greift auf inbox.js-Globals (state, _toField, _ccField, escHtml, _escHtml,
// loadSmtpServers, openEmail, renderKiRefineBar, _renderComposeAttachments,
// _clearBulkMode, _bulkRecipients, _bulkTracking, _bulkApplyResult,
// _sendBulk) und auf api.* zu. Shared script-scope, Auflösung zur Laufzeit.

// ── Versand-Benachrichtigungen ───────────────────────────────

const _sendNotifContainer = document.getElementById('send-notifications');

function _addSendNotif(jobId, to, subject) {
  const shortTo      = to.length > 35 ? to.slice(0, 35) + '…' : to;
  const shortSubject = subject.length > 40 ? subject.slice(0, 40) + '…' : subject;

  const el = document.createElement('div');
  el.className = 'send-notif pending';
  el.dataset.jobId = jobId;
  el.innerHTML = `
    <span class="send-notif-icon">⏳</span>
    <span class="send-notif-text">Wird gesendet an <strong>${_escHtml(shortTo)}</strong> — ${_escHtml(shortSubject)}</span>
  `;
  _sendNotifContainer.appendChild(el);
}

function _handleSendResult(data) {
  // Bulk-Hook: wenn die job_id zu einem laufenden Massenversand gehört,
  // übernimmt das Status-Modal die Anzeige — keine normale Notif.
  if (_bulkTracking && _bulkTracking.byJobId.has(data.job_id)) {
    _bulkApplyResult(data);
    return;
  }
  const el = _sendNotifContainer.querySelector(`[data-job-id="${data.job_id}"]`);
  if (!el) return;

  const shortTo      = (data.to      || '').length > 35 ? data.to.slice(0, 35) + '…' : (data.to || '');
  const shortSubject = (data.subject || '').length > 40 ? data.subject.slice(0, 40) + '…' : (data.subject || '');

  if (data.success) {
    el.className = 'send-notif success';
    el.innerHTML = `
      <span class="send-notif-icon">✓</span>
      <span class="send-notif-text">Gesendet an <strong>${_escHtml(shortTo)}</strong> — ${_escHtml(shortSubject)}</span>
    `;
    setTimeout(() => el.remove(), 4000);
  } else {
    const errMsg = (data.error || 'Unbekannter Fehler').slice(0, 80);
    el.className = 'send-notif error';
    const dismiss = document.createElement('button');
    dismiss.className = 'send-notif-dismiss';
    dismiss.title = 'Schließen';
    dismiss.textContent = '×';
    dismiss.onclick = () => el.remove();
    el.innerHTML = `
      <span class="send-notif-icon">✗</span>
      <span class="send-notif-text">Fehler an <strong>${_escHtml(shortTo)}</strong> — ${_escHtml(shortSubject)}: ${_escHtml(errMsg)}</span>
    `;
    el.appendChild(dismiss);
  }
}

// URLs in Textknoten zu klickbaren <a>-Tags machen. Bestehende <a> bleiben unangetastet.
function _linkifyHtml(html) {
  if (!html) return html;
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      let p = node.parentNode;
      while (p && p !== tpl.content) {
        const name = p.nodeName;
        if (name === 'A' || name === 'SCRIPT' || name === 'STYLE') return NodeFilter.FILTER_REJECT;
        p = p.parentNode;
      }
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const URL_RE = /(https?:\/\/[^\s<>"']+|www\.[^\s<>"']+)/g;
  const targets = [];
  let n;
  while ((n = walker.nextNode())) {
    if (n.nodeValue && URL_RE.test(n.nodeValue)) targets.push(n);
    URL_RE.lastIndex = 0;
  }
  for (const textNode of targets) {
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let lastIdx = 0;
    URL_RE.lastIndex = 0;
    let m;
    while ((m = URL_RE.exec(text))) {
      let url = m[0];
      const trail = url.match(/[.,;:!?)\]}>]+$/);
      if (trail) url = url.slice(0, -trail[0].length);
      if (m.index > lastIdx) {
        frag.appendChild(document.createTextNode(text.slice(lastIdx, m.index)));
      }
      const a = document.createElement('a');
      a.href = url.startsWith('www.') ? 'https://' + url : url;
      a.textContent = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      frag.appendChild(a);
      lastIdx = m.index + url.length;
      URL_RE.lastIndex = lastIdx;
    }
    if (lastIdx < text.length) {
      frag.appendChild(document.createTextNode(text.slice(lastIdx)));
    }
    textNode.parentNode.replaceChild(frag, textNode);
  }
  return tpl.innerHTML;
}

// ── Compose-Toolbar ──────────────────────────────────────────
function setupComposeToolbar() {
  // mousedown statt click, damit der Editor nicht die Fokussierung verliert
  document.getElementById('tb-bold').addEventListener('mousedown', (e) => {
    e.preventDefault();
    document.execCommand('bold', false, null);
    document.getElementById('ci-body').focus();
  });
  document.getElementById('tb-underline').addEventListener('mousedown', (e) => {
    e.preventDefault();
    document.execCommand('underline', false, null);
    document.getElementById('ci-body').focus();
  });
  document.querySelectorAll('#tb-fontsize-group .tbfs').forEach(btn => {
    btn.addEventListener('mousedown', (e) => {
      e.preventDefault(); // Fokus im Editor behalten
    });
    btn.addEventListener('click', (e) => {
      document.execCommand('fontSize', false, btn.dataset.size);
      document.getElementById('ci-body').focus();
      document.querySelectorAll('#tb-fontsize-group .tbfs').forEach(b => b.classList.remove('tbfs-active'));
      btn.classList.add('tbfs-active');
    });
  });

  // Link-Button: Selektion in <a> wickeln, ohne Selektion: <a>URL</a> an Cursor
  const linkBtn = document.getElementById('tb-link');
  if (linkBtn) {
    let _savedRange = null;
    linkBtn.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const sel = window.getSelection();
      _savedRange = (sel && sel.rangeCount > 0) ? sel.getRangeAt(0).cloneRange() : null;
    });
    linkBtn.addEventListener('click', () => {
      const body = document.getElementById('ci-body');
      const raw = prompt('Link-URL eingeben:', 'https://');
      if (!raw) { body.focus(); return; }
      const trimmed = raw.trim();
      const finalUrl = (/^(https?:\/\/|mailto:|tel:)/i.test(trimmed))
        ? trimmed
        : 'https://' + trimmed;

      body.focus();
      const sel = window.getSelection();
      if (_savedRange) {
        sel.removeAllRanges();
        sel.addRange(_savedRange);
      }
      const range = (sel && sel.rangeCount > 0) ? sel.getRangeAt(0) : null;

      const a = document.createElement('a');
      a.href = finalUrl;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';

      if (range && !range.collapsed && body.contains(range.commonAncestorContainer)) {
        a.appendChild(range.extractContents());
        range.insertNode(a);
      } else {
        a.textContent = finalUrl;
        if (range && body.contains(range.commonAncestorContainer)) {
          range.insertNode(a);
        } else {
          body.appendChild(a);
        }
      }
      // Cursor hinter den Link setzen
      const after = document.createRange();
      after.setStartAfter(a);
      after.collapse(true);
      sel.removeAllRanges();
      sel.addRange(after);
      _savedRange = null;
    });
  }

  // Sans-Serif-Button: alle font-family-Styles und <font face>-Reste im Body entfernen
  const sansBtn = document.getElementById('tb-sansserif');
  if (sansBtn) {
    sansBtn.addEventListener('mousedown', (e) => e.preventDefault());
    sansBtn.addEventListener('click', () => {
      const body = document.getElementById('ci-body');
      body.querySelectorAll('[style*="font-family" i], [style*="font:" i]').forEach(el => {
        el.style.fontFamily = '';
        if (!el.getAttribute('style')) el.removeAttribute('style');
      });
      body.querySelectorAll('font[face]').forEach(el => {
        el.removeAttribute('face');
      });
      body.focus();
    });
  }

  // Emoji-Buttons: fügen das Emoji als großes <span> am Cursor ein
  document.querySelectorAll('#tb-emoji-group .tb-emoji').forEach(btn => {
    btn.addEventListener('mousedown', (e) => e.preventDefault());
    btn.addEventListener('click', () => {
      const body = document.getElementById('ci-body');
      const span = document.createElement('span');
      span.style.fontSize = '48px';
      span.style.lineHeight = '1';
      span.textContent = btn.dataset.emoji;

      const sel = window.getSelection();
      const range = (sel && sel.rangeCount > 0 && body.contains(sel.anchorNode))
        ? sel.getRangeAt(0)
        : null;

      if (range) {
        range.deleteContents();
        range.insertNode(span);
      } else {
        body.appendChild(span);
      }
      // Cursor hinter den Span setzen, damit nachfolgender Text wieder normal groß ist
      const after = document.createRange();
      after.setStartAfter(span);
      after.collapse(true);
      sel.removeAllRanges();
      sel.addRange(after);
      body.focus();
    });
  });
}

// ── Bestätigungs-Dialog ──────────────────────────────────────
function confirmDiscard(msg) {
  return new Promise(resolve => {
    document.getElementById('confirm-msg').textContent = msg;
    const overlay = document.getElementById('confirm-overlay');
    overlay.style.display = 'flex';
    const cleanup = (result) => {
      overlay.style.display = 'none';
      resolve(result);
    };
    document.getElementById('confirm-ok').onclick     = () => cleanup(true);
    document.getElementById('confirm-cancel').onclick = () => cleanup(false);
  });
}

function composeHasContent() {
  if (document.getElementById('detail-tabs').style.display === 'none') return false;
  const body    = document.getElementById('ci-body').innerText || '';
  const to      = _toField.getAddresses().join(', ');
  const subject = document.getElementById('ci-subject').value.trim();
  // Hat der Nutzer etwas eingegeben? Signatur alleine zählt nicht als Inhalt.
  const sig = (() => {
    const acc = state.accounts.find(a => a.id === state.activeAccount);
    return acc && acc.signature ? acc.signature.trim() : '';
  })();
  const bodyWithoutSig = body.replace(sig, '').trim();
  return !!(to || subject || bodyWithoutSig);
}

// ── Inline Compose State ─────────────────────────────────────
let _draftId = null;
let _draftTimer = null;
let _editingDraftItemEl = null; // DOM-Element des Draft-Eintrags in der Liste (für Refresh)
let _composeAttachments = [];   // [{id, filename, size}] — temporäre Uploads
let _replyToEmailId = null;     // ID der E-Mail, auf die geantwortet wird (für is_answered)

async function openCompose({ to = '', subject = '', body = null, quote = '', quoteHtml = '', fromAccountId = null, existingDraftId = null, replyToEmailId = null, replyToFromEmail = null } = {}) {
  // Wenn bereits ein Entwurf mit Inhalt offen ist → nachfragen
  if (composeHasContent()) {
    const discard = await confirmDiscard(
      'Du hast einen offenen Entwurf. Wenn du fortfährst, geht er verloren.'
    );
    if (!discard) {
      showTab('compose'); // zurück zum offenen Entwurf
      return false;  // Abgebrochen
    }
  }

  // Tab-Leiste einblenden und benennen
  const tabsEl = document.getElementById('detail-tabs');
  tabsEl.style.display = 'flex';
  document.getElementById('tab-compose').textContent = subject || 'Neue E-Mail';

  showTab('compose');

  // Von-Dropdown befüllen
  const fromSel = document.getElementById('ci-from-account');
  fromSel.innerHTML = state.accounts.map(a =>
    `<option value="${a.id}">${escHtml(a.from_name || a.name || a.from_email)} &lt;${escHtml(a.from_email)}&gt;</option>`
  ).join('');
  fromSel.value = fromAccountId || state.activeAccount || (state.accounts[0]?.id ?? '');

  // SMTP-Dropdown befüllen (ggf. nachladen wenn beim Start fehlgeschlagen)
  if (state.smtpServers.length === 0) await loadSmtpServers();
  const smtpSel = document.getElementById('ci-smtp-server');
  if (state.smtpServers.length > 0) {
    smtpSel.innerHTML = state.smtpServers.map(s =>
      `<option value="${s.id}">${escHtml(s.name)}</option>`
    ).join('');
    const defaultSmtp = state.smtpServers.find(s => s.is_default) || state.smtpServers[0];
    smtpSel.value = defaultSmtp.id;
  } else {
    smtpSel.innerHTML = '<option value="">— kein SMTP-Server —</option>';
  }

  _toField.setAddresses(to ? [to] : []);
  _ccField.clear();
  document.getElementById('ci-subject').value = subject;

  const replytoWarn = document.getElementById('ci-replyto-warning');
  if (replyToFromEmail) {
    replytoWarn.textContent = `Hinweis: Diese E-Mail wird an die Reply-To-Adresse gesendet (${to}), nicht an die Absenderadresse (${replyToFromEmail}).`;
    replytoWarn.style.display = 'block';
  } else {
    replytoWarn.textContent = '';
    replytoWarn.style.display = 'none';
  }

  // Body: explizit übergeben (bei Draft-Bearbeitung) oder Signatur einsetzen (neue E-Mail)
  const account = state.accounts.find(a => a.id === state.activeAccount);
  const sig = account && account.signature ? account.signature.trim() : '';
  const bodyEl = document.getElementById('ci-body');
  if (body !== null) {
    // Plain-Text-Body (aus Draft) → als HTML setzen
    bodyEl.innerHTML = escHtml(body).replace(/\n/g, '<br>');
  } else {
    // Neue E-Mail: Signatur als HTML
    const sigHtml = sig
      ? `<br><br><span>${escHtml(sig).replace(/\n/g, '<br>')}</span>`
      : '';
    bodyEl.innerHTML = sigHtml;
  }

  const quoteEl = document.getElementById('ci-quote');
  quoteEl.classList.remove('has-html');
  delete quoteEl.dataset.quoteHtml;
  if (quoteHtml) {
    // HTML-Dokument auf Body-Inhalt reduzieren, Scripts entfernen
    const _bodyMatch = quoteHtml.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
    const _htmlContent = (_bodyMatch ? _bodyMatch[1] : quoteHtml)
      .replace(/<script\b[\s\S]*?<\/script>/gi, '');
    quoteEl.innerHTML = _htmlContent;
    quoteEl.dataset.quoteHtml = quoteHtml;
    quoteEl.classList.add('has-html');
    quoteEl.style.display = 'block';
  } else if (quote) {
    quoteEl.textContent = quote;
    quoteEl.style.display = 'block';
  } else {
    quoteEl.innerHTML = '';
    quoteEl.style.display = 'none';
  }

  const statusEl = document.getElementById('draft-status');
  statusEl.textContent = '';
  statusEl.style.color = '';
  document.getElementById('btn-send-inline').disabled = false;
  _draftId = existingDraftId; // null → neuer Draft per POST; ID → bestehender Draft per PATCH
  _replyToEmailId = replyToEmailId;

  // Anhang-Liste zurücksetzen (bestehende Uploads aus Speicher löschen)
  _composeAttachments.forEach(a => api.deleteUpload(a.id).catch(() => {}));
  _composeAttachments = [];
  _renderComposeAttachments();

  // Cancel-Button-Text je nach Modus
  const cancelBtn = document.getElementById('btn-compose-cancel');
  cancelBtn.textContent = existingDraftId ? 'Draft speichern' : 'Abbrechen';

  // Cursor an den Anfang des Bodys setzen (Chrome springt sonst ans Ende)
  bodyEl.focus();
  try {
    const range = document.createRange();
    range.setStart(bodyEl, 0);
    range.collapse(true);
    const sel = window.getSelection();
    if (sel) { sel.removeAllRanges(); sel.addRange(range); }
  } catch (_) {}
  // Bei neuer E-Mail (kein Empfänger gesetzt): Fokus ins To-Feld.
  // Bei Antwort/Draft mit Empfänger: Fokus bleibt im Body.
  if (!to) {
    document.getElementById('ci-to-input').focus();
  }
  // Nach dem Rendern an den Anfang scrollen (requestAnimationFrame überschreibt Browser-Autoscroll)
  requestAnimationFrame(() => {
    document.getElementById('compose-mode').scrollTop = 0;
  });

  // KI-Refinement-Bar rendern (nur sichtbar wenn KI-Modus aktiv)
  renderKiRefineBar();

  // Tab im Subject-Feld → direkt ins Body-Feld springen
  const subjectEl = document.getElementById('ci-subject');
  const subjectTabHandler = (e) => {
    if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault();
      document.getElementById('ci-body').focus();
    }
  };
  subjectEl.removeEventListener('keydown', subjectEl._tabHandler);
  subjectEl._tabHandler = subjectTabHandler;
  subjectEl.addEventListener('keydown', subjectTabHandler);

  // Tab im To-Feld → CC überspringen, direkt ins Betreff-Feld.
  // (Tab im Betreff springt dann weiter ins Body-Feld.)
  // Läuft NACH dem Address-Field-Handler (Autocomplete/Chip-Logik bleibt erhalten).
  const toInputEl = document.getElementById('ci-to-input');
  const toTabHandler = (e) => {
    if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault();
      // Wichtig für Text-Expander (z.B. Rocket Typist): Der Empfängertext muss
      // synchron als Chip übernommen werden, bevor der Fokus ins Betreff-Feld
      // springt. Nur auf blur zu warten ist bei schnell folgenden Tab/Text-
      // Events zu spät und kann das erste Snippet-Feld verlieren.
      _toField.commitPending();
      scheduleDraftSave();
      document.getElementById('ci-subject').focus();
    }
  };
  toInputEl.removeEventListener('keydown', toInputEl._tabHandler);
  toInputEl._tabHandler = toTabHandler;
  toInputEl.addEventListener('keydown', toTabHandler);

  // Auto-Save beim Tippen (alte Listener zuerst entfernen, damit keine Duplikate entstehen)
  ['ci-to-input', 'ci-cc-input', 'ci-subject'].forEach(id => {
    const el = document.getElementById(id);
    el.removeEventListener('input', scheduleDraftSave);
    el.addEventListener('input', scheduleDraftSave);
  });
  bodyEl.removeEventListener('input', scheduleDraftSave);
  bodyEl.addEventListener('input', scheduleDraftSave);

  // Bestehender Draft: kein sofortiger Save (Draft existiert bereits, wird beim Tippen per PATCH aktualisiert)
  // Neue Compose: sofort als Draft anlegen
  if (!existingDraftId) {
    await saveDraft();
  }
}

async function closeCompose() {
  clearTimeout(_draftTimer);

  // Wenn ein bestehender Draft bearbeitet wurde: jetzt final speichern und Liste aktualisieren
  if (_draftId && _editingDraftItemEl) {
    await saveDraft();
    // Draft-Eintrag in der Liste und im State mit neuen Daten aktualisieren
    try {
      const updated = await api.getEmail(_draftId);
      const stateEmail = state.emails.find(e => e.id === _draftId);
      if (stateEmail) {
        stateEmail.subject = updated.subject;
        stateEmail.snippet = updated.snippet;
        // Listeneintrag-Text aktualisieren
        const subjectEl = _editingDraftItemEl.querySelector('.email-subject');
        const snippetEl = _editingDraftItemEl.querySelector('.email-snippet');
        if (subjectEl) subjectEl.textContent = updated.subject || '(kein Betreff)';
        if (snippetEl) snippetEl.textContent = updated.snippet || '';
      }
      // Detail-Panel mit aktualisiertem Inhalt öffnen
      openEmail(stateEmail || updated, _editingDraftItemEl);
    } catch (_) {}
    _editingDraftItemEl = null;
  }

  _replyToEmailId = null;
  const _w = document.getElementById('ci-replyto-warning');
  _w.textContent = '';
  _w.style.display = 'none';
  document.getElementById('btn-compose-cancel').textContent = 'Abbrechen';
  document.getElementById('detail-tabs').style.display = 'none';
  showTab('email');
  // Massenversand-Modus zurücksetzen
  _clearBulkMode();
}

function scheduleDraftSave() {
  clearTimeout(_draftTimer);
  _draftTimer = setTimeout(saveDraft, 1000);
  document.getElementById('draft-status').textContent = 'Nicht gespeichert';
}

async function saveDraft() {
  const to          = _toField.getAddresses().join(', ');
  const cc          = _ccField.getAddresses().join(', ');
  const subject     = document.getElementById('ci-subject').value.trim();
  const ciBody      = document.getElementById('ci-body');
  const body        = ciBody.innerText || '';
  const body_html   = ciBody.innerHTML || '';
  const _quoteEl    = document.getElementById('ci-quote');
  const quote       = _quoteEl.textContent;
  const quote_html  = _quoteEl.dataset.quoteHtml || '';
  const from_account = document.getElementById('ci-from-account').value;

  if (!to && !subject && !body.trim()) return;
  if (!from_account) return;

  const statusEl = document.getElementById('draft-status');
  statusEl.style.color = '';
  statusEl.textContent = 'Speichert…';

  try {
    const result = await api.saveDraft({ id: _draftId, to, cc, subject, body, body_html, quote, quote_html, from_account });
    if (result && result.id) _draftId = result.id;
    statusEl.textContent = 'Entwurf gespeichert';
    setTimeout(() => {
      // Nur löschen wenn seitdem keine neue Meldung (z.B. Validierungsfehler) reingeschrieben wurde
      if (statusEl.textContent === 'Entwurf gespeichert') statusEl.textContent = '';
    }, 2000);
  } catch (e) {
    statusEl.textContent = '';
    console.warn('saveDraft fehlgeschlagen:', e.message);
  }
}

document.getElementById('btn-compose-cancel').addEventListener('click', closeCompose);

document.getElementById('btn-send-inline').addEventListener('click', async () => {
  // Draft-Timer sofort stoppen — verhindert dass ein ausstehender Save nach dem Senden feuert
  clearTimeout(_draftTimer);

  // Bulk-Modus zweigt früh ab — verwendet eigene Validierung und Status-UI.
  if (_bulkRecipients.length > 0) {
    await _sendBulk();
    return;
  }

  const to         = _toField.getAddresses().join(', ');
  const cc         = _ccField.getAddresses().join(', ');
  const subject    = document.getElementById('ci-subject').value.trim();
  const ciBodyEl   = document.getElementById('ci-body');
  const body       = (ciBodyEl.innerText || '').trim();
  const body_html  = _linkifyHtml(ciBodyEl.innerHTML || '');
  const _qEl       = document.getElementById('ci-quote');
  const quote      = _qEl.textContent;
  const quote_html = _qEl.dataset.quoteHtml || '';
  const fromAccId  = document.getElementById('ci-from-account').value;
  const smtpId     = document.getElementById('ci-smtp-server').value;
  const statusEl   = document.getElementById('draft-status');

  if (!to || !subject) {
    statusEl.textContent = 'Bitte Empfänger und Betreff ausfüllen.';
    statusEl.style.color = 'var(--danger)';
    // Cursor in das leere Feld setzen — der Statustext allein neben dem
    // Senden-Button wird leicht übersehen.
    if (!to) document.getElementById('ci-to-input')?.focus();
    else document.getElementById('ci-subject')?.focus();
    return;
  }

  // Beantwortet-Symbol sofort im UI aktualisieren (optimistic)
  const sentReplyToId = _replyToEmailId;
  if (sentReplyToId) {
    const local = state.emails.find(e => e.id === sentReplyToId);
    if (local) local.is_answered = true;
    const itemEl = document.querySelector(`.email-item[data-id="${sentReplyToId}"]`);
    itemEl?.querySelector('.flag-answered')?.classList.add('active');
  }

  const attachment_ids   = _composeAttachments.map(a => a.id);
  const draftIdToDelete  = _draftId;

  // Compose sofort schließen — Versand läuft im Hintergrund weiter
  _composeAttachments = [];
  _editingDraftItemEl = null;
  closeCompose();

  try {
    const res = await api.sendEmail({
      to, cc, subject, body, body_html, quote, quote_html,
      from_account: fromAccId, smtp_server: smtpId,
      attachment_ids, in_reply_to_email_id: sentReplyToId,
      draft_id: draftIdToDelete,
    });
    // Notification-Zeile anlegen (SSE-Event aktualisiert sie später)
    if (res && res.job_id) {
      _addSendNotif(res.job_id, to, subject);
    }
  } catch (e) {
    // Validierungsfehler vom Backend (400/502) — sofort als Fehler anzeigen
    const jobId = 'err-' + Date.now();
    _addSendNotif(jobId, to, subject);
    _handleSendResult({ job_id: jobId, success: false, to, subject, error: e.message });
  }
});
