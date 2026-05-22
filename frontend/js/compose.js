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


// ── Massenversand ───────────────────────────────────────────
//
// Im Compose-Modus öffnet der Button "Massenversand" ein Modal mit einer
// Textarea für E-Mail-Adressen (eine pro Zeile). Bei Übernahme ersetzt ein
// Banner das normale "An"-Feld. Beim Senden ruft das Frontend
// `/emails/bulk-send` auf; das Backend versendet je Empfänger eine eigene
// Mail mit 5 s Abstand. SSE-Events `send-result` aktualisieren live ein
// Status-Modal mit ✓/✗ pro Adresse — inklusive Retry- und Copy-Funktion.

let _bulkRecipients = [];
let _bulkTracking = null;  // { byJobId: Map<jobId,addr>, byAddr: Map<addr,row>, compose: {...} }
const _EMAIL_RE = /^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$/;

function _parseBulkInput(text) {
  const valid = [], invalid = [], seen = new Set();
  (text || '').split(/[\n,;]+/).forEach(line => {
    const addr = line.trim();
    if (!addr) return;
    const m = addr.match(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/);
    if (!m || !_EMAIL_RE.test(m[0])) { invalid.push(addr); return; }
    const key = m[0].toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    valid.push(addr);
  });
  return { valid, invalid };
}

function _renderBulkBanner() {
  const banner = document.getElementById('ci-bulk-banner');
  const toField = document.getElementById('ci-to-field');
  if (_bulkRecipients.length === 0) {
    banner.style.display = 'none';
    toField.style.display = '';
    return;
  }
  banner.querySelector('.ci-bulk-banner-text').innerHTML =
    `Massenversand aktiv: <strong>${_bulkRecipients.length}</strong> Empfänger`;
  banner.style.display = 'flex';
  toField.style.display = 'none';
}

function _clearBulkMode() {
  _bulkRecipients = [];
  _renderBulkBanner();
}

function _openBulkModal() {
  const overlay  = document.getElementById('bulk-modal-overlay');
  const textarea = document.getElementById('bulk-modal-textarea');
  const errors   = document.getElementById('bulk-modal-errors');
  const info     = document.getElementById('bulk-modal-info');
  textarea.value = _bulkRecipients.join('\n');
  errors.style.display = 'none';
  errors.textContent = '';
  if (info) info.textContent = '';
  overlay.style.display = 'flex';
  setTimeout(() => textarea.focus(), 0);
}

// Resend-Helper: wird vom Aussendungs-Tab aufgerufen. Wechselt zum Inbox-Tab,
// oeffnet Compose mit Subject + body_html aus der Original-Aussendung,
// setzt SMTP wenn moeglich und oeffnet das Bulk-Modal mit den ausgewaehlten
// Empfaengern vorgefuellt — Stefan kann dort SMTP wechseln und absenden.
window.mfComposeResend = {
  async open({ subject, body_html, body_text, recipients, from_account, smtp_server } = {}) {
    if (window.mfTabs?.setActiveTab) {
      window.mfTabs.setActiveTab('inbox');
    }
    const accountId = (from_account && state.accounts.find(a => a.id === from_account))
      ? from_account
      : null;
    const opened = await openCompose({
      subject: subject || '',
      fromAccountId: accountId,
    });
    if (opened === false) return false;
    // Body als HTML setzen (openCompose hat nur Plain-body-Support)
    const bodyEl = document.getElementById('ci-body');
    if (bodyEl && body_html) {
      bodyEl.innerHTML = body_html;
    } else if (bodyEl && body_text) {
      bodyEl.innerHTML = _escHtml(body_text).replace(/\n/g, '<br>');
    }
    // SMTP-Server vorwaehlen falls vorhanden — Stefan kann im Dropdown wechseln
    if (smtp_server) {
      const smtpSel = document.getElementById('ci-smtp-server');
      if (smtpSel && smtpSel.querySelector(`option[value="${smtp_server}"]`)) {
        smtpSel.value = smtp_server;
      }
    }
    // Bulk-Mode aktivieren + Modal sofort oeffnen
    _bulkRecipients = (recipients || []).map(r => String(r).trim()).filter(Boolean);
    _renderBulkBanner();
    _openBulkModal();
    return true;
  },
};

function _closeBulkModal() {
  document.getElementById('bulk-modal-overlay').style.display = 'none';
}

document.getElementById('btn-test-send').addEventListener('click', async () => {
  const a = auth.getAuth();
  const userEmail = a?.record?.email;
  const userName  = a?.record?.name || a?.record?.email || '';
  if (!userEmail) {
    alert('Test-Versand nicht möglich: keine eingeloggte E-Mail-Adresse gefunden.');
    return;
  }
  const subject   = document.getElementById('ci-subject').value.trim();
  const ciBodyEl  = document.getElementById('ci-body');
  const body      = (ciBodyEl.innerText || '').trim();
  const body_html = _linkifyHtml(ciBodyEl.innerHTML || '');
  const fromAccId = document.getElementById('ci-from-account').value;
  const smtpId    = document.getElementById('ci-smtp-server').value;
  const statusEl  = document.getElementById('draft-status');
  if (!subject) {
    statusEl.textContent = 'Bitte Betreff ausfüllen für den Test-Versand.';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  // {{name}} / {{email}} clientseitig durch User-Daten ersetzen — Backend
  // wuerde sonst contacts-Lookup machen und ggf. leeren Namen einsetzen.
  const namePh  = /\{\{\s*name\s*\}\}/g;
  const emailPh = /\{\{\s*email\s*\}\}/g;
  const subjectFilled = subject.replace(namePh, userName).replace(emailPh, userEmail);
  const bodyFilled    = body.replace(namePh, userName).replace(emailPh, userEmail);
  const htmlFilled    = body_html.replace(namePh, userName).replace(emailPh, userEmail);

  const btn = document.getElementById('btn-test-send');
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = 'Sende…';
  try {
    await api.sendEmail({
      to: userEmail,
      subject: '[TEST] ' + subjectFilled,
      body: bodyFilled,
      body_html: htmlFilled,
      from_account: fromAccId,
      smtp_server: smtpId,
      attachment_ids: _composeAttachments.map(a => a.id),
    });
    alert(`Test-E-Mail wurde an ${userEmail} versendet.`);
  } catch (err) {
    alert('Test-Versand fehlgeschlagen: ' + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = origLabel;
  }
});

document.getElementById('btn-bulk').addEventListener('click', _openBulkModal);
document.getElementById('ci-bulk-edit').addEventListener('click', _openBulkModal);
document.getElementById('ci-bulk-clear').addEventListener('click', _clearBulkMode);
document.getElementById('bulk-modal-close').addEventListener('click', _closeBulkModal);
document.getElementById('bulk-modal-cancel').addEventListener('click', _closeBulkModal);
document.getElementById('bulk-modal-overlay').addEventListener('click', (e) => {
  if (e.target.id === 'bulk-modal-overlay') _closeBulkModal();
});

function _setBulkInfo(msg, isError = false) {
  const el = document.getElementById('bulk-modal-info');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = isError ? 'var(--danger)' : 'var(--text2)';
}

async function _bulkAddGroupMembers(group) {
  let members;
  try {
    members = await api.contactGroups.members(group.id);
  } catch (err) {
    _setBulkInfo(`Mitglieder laden fehlgeschlagen: ${err.message || err}`, true);
    return;
  }
  const subscribed = members.filter(m => m.email && !m.unsubscribed);
  const unsubCount = members.length - subscribed.length;
  if (subscribed.length === 0) {
    _setBulkInfo(`Gruppe „${group.name}" hat keine versendbaren Mitglieder${unsubCount ? ` (${unsubCount} unsubscribed)` : ''}.`, true);
    return;
  }
  const textarea = document.getElementById('bulk-modal-textarea');
  const existing = new Set(
    (textarea.value || '').split(/[\n,;]+/).map(l => l.trim().toLowerCase()).filter(Boolean)
  );
  const newAddrs = [];
  let duplicate = 0;
  subscribed.forEach(m => {
    const email = (m.email || '').trim();
    if (!email) return;
    if (existing.has(email.toLowerCase())) { duplicate++; return; }
    newAddrs.push(email);
    existing.add(email.toLowerCase());
  });
  const sep = textarea.value && !textarea.value.endsWith('\n') ? '\n' : '';
  textarea.value = textarea.value + sep + newAddrs.join('\n');
  const parts = [`Gruppe „${group.name}": ${newAddrs.length} ergänzt`];
  if (duplicate)  parts.push(`${duplicate} doppelt`);
  if (unsubCount) parts.push(`${unsubCount} unsubscribed`);
  _setBulkInfo(parts.join(' · '));
}

document.getElementById('bulk-modal-add-group').addEventListener('click', (e) => {
  mfDropdown.open({
    trigger: e.currentTarget,
    searchPlaceholder: 'Gruppe suchen…',
    emptyText: 'Keine Kontakt-Gruppen angelegt.',
    loadItems: async () => {
      const groups = await api.contactGroups.list();
      groups.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
      return groups.map(g => ({
        label: g.name,
        sublabel: g.description || '',
        value: g,
      }));
    },
    onSelect: (item) => _bulkAddGroupMembers(item.value),
  });
});

document.getElementById('bulk-modal-apply').addEventListener('click', () => {
  const textarea = document.getElementById('bulk-modal-textarea');
  const errors   = document.getElementById('bulk-modal-errors');
  const { valid, invalid } = _parseBulkInput(textarea.value);
  if (invalid.length > 0) {
    errors.style.display = 'block';
    errors.textContent = 'Ungültige Adressen:\n' + invalid.join('\n');
    return;
  }
  if (valid.length === 0) {
    errors.style.display = 'block';
    errors.textContent = 'Bitte mindestens eine gültige Adresse eingeben.';
    return;
  }
  _bulkRecipients = valid;
  _renderBulkBanner();
  _closeBulkModal();
});

async function _sendBulk() {
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

  if (!subject) {
    statusEl.textContent = 'Bitte Betreff ausfüllen.';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  if (_bulkRecipients.length === 0) {
    statusEl.textContent = 'Massenversand-Liste ist leer.';
    statusEl.style.color = 'var(--danger)';
    return;
  }

  const composeSnapshot = {
    subject, body, body_html, quote, quote_html,
    from_account: fromAccId, smtp_server: smtpId,
    attachment_ids: _composeAttachments.map(a => a.id),
    draft_id: _draftId,
  };
  const recipients = _bulkRecipients.slice();

  _composeAttachments = [];
  _editingDraftItemEl = null;
  closeCompose();

  _bulkTracking = { byJobId: new Map(), byAddr: new Map(), compose: composeSnapshot,
                    bulkSendId: null, pollTimer: null };
  _openBulkStatusModal();
  await _bulkStart(recipients, composeSnapshot);
}

async function _bulkStart(recipients, snapshot) {
  recipients.forEach(addr => _bulkUpsertRow(addr, { status: 'queued', error: null, jobId: null }));
  _bulkUpdateSummary();

  let resp;
  try {
    resp = await api.bulkSendEmail({ ...snapshot, recipients, delay_seconds: 5 });
  } catch (e) {
    recipients.forEach(addr => _bulkUpsertRow(addr, { status: 'error', error: e.message, jobId: null }));
    _bulkFinalize();
    return;
  }
  (resp.jobs || []).forEach(j => {
    _bulkTracking.byJobId.set(j.job_id, j.to);
    _bulkUpsertRow(j.to, { status: 'sending', error: null, jobId: j.job_id });
  });
  _bulkRenderFilteredBanner(resp.filtered_out || []);
  _bulkUpdateSummary();

  // Polling-Fallback gegen SSE-Loss bei Backend-Restart (B15).
  // SSE bleibt der primäre Realtime-Pfad; Polling holt nur verpasste
  // Endstatus-Übergänge aus bulk_sends.recipients[] nach.
  if (resp.bulk_send_id) {
    _bulkTracking.bulkSendId = resp.bulk_send_id;
    _bulkTracking.pollTimer = setInterval(_bulkPollOnce, 5_000);
  }
}

async function _bulkPollOnce() {
  if (!_bulkTracking || !_bulkTracking.bulkSendId) return;
  let rec;
  try {
    rec = await api.bulkSends.get(_bulkTracking.bulkSendId);
  } catch (_) {
    return;  // Netzwerk wackelig — beim nächsten Tick erneut.
  }
  if (!_bulkTracking) return;  // Modal in der Zwischenzeit geschlossen
  const recipients = rec.recipients || [];
  for (const r of recipients) {
    const addr = r.raw || r.email;
    if (!addr) continue;
    const cur = _bulkTracking.byAddr.get(addr);
    const pbStatus = r.status;
    let uiStatus = null;
    let err = null;
    if (pbStatus === 'sent')         uiStatus = 'success';
    else if (pbStatus === 'bounced') { uiStatus = 'error'; err = r.error || 'Bounce'; }
    else if (pbStatus === 'error')   { uiStatus = 'error'; err = r.error || 'Fehler'; }
    if (uiStatus && (!cur || cur.status !== uiStatus)) {
      _bulkUpsertRow(addr, { status: uiStatus, error: err });
    }
  }
  _bulkUpdateSummary();
  const allDone = Array.from(_bulkTracking.byAddr.values())
    .every(r => r.status === 'success' || r.status === 'error');
  if (allDone) _bulkFinalize();
}

function _bulkApplyResult(data) {
  const addr = _bulkTracking.byJobId.get(data.job_id);
  if (!addr) return;
  _bulkUpsertRow(addr, {
    status: data.success ? 'success' : 'error',
    error: data.success ? null : (data.error || 'Unbekannter Fehler'),
    jobId: data.job_id,
  });
  _bulkUpdateSummary();
  const allDone = Array.from(_bulkTracking.byAddr.values())
    .every(r => r.status === 'success' || r.status === 'error');
  if (allDone) _bulkFinalize();
}

function _bulkUpsertRow(addr, patch) {
  const prev = _bulkTracking.byAddr.get(addr) || {};
  _bulkTracking.byAddr.set(addr, { ...prev, ...patch, addr });
  _bulkRenderList();
}

function _bulkRenderList() {
  const listEl = document.getElementById('bulk-status-list');
  // Sortierung: Erfolge oben, dann sending/queued, Fehler ganz unten (zum Rauskopieren).
  const order = { success: 0, sending: 1, queued: 2, error: 3 };
  const rows = Array.from(_bulkTracking.byAddr.values())
    .sort((a, b) => (order[a.status] - order[b.status]) || a.addr.localeCompare(b.addr));
  listEl.innerHTML = rows.map(r => {
    let icon, cls;
    if (r.status === 'success')      { icon = '✓'; cls = 'success'; }
    else if (r.status === 'error')   { icon = '✗'; cls = 'error';   }
    else if (r.status === 'sending') { icon = '⏳'; cls = 'sending'; }
    else                              { icon = '·'; cls = 'queued';  }
    const msg = r.status === 'error' && r.error
      ? `<span class="bulk-status-msg" title="${_escHtml(r.error)}">${_escHtml(r.error)}</span>`
      : '';
    return `<div class="bulk-status-row ${cls}"><span class="bulk-status-icon">${icon}</span><span class="bulk-status-addr">${_escHtml(r.addr)}</span>${msg}</div>`;
  }).join('');
}

function _bulkUpdateSummary() {
  const rows = Array.from(_bulkTracking.byAddr.values());
  const total = rows.length;
  const ok    = rows.filter(r => r.status === 'success').length;
  const err   = rows.filter(r => r.status === 'error').length;
  const pending = total - ok - err;
  const titleEl = document.getElementById('bulk-status-title');
  const sumEl   = document.getElementById('bulk-status-summary');
  if (pending > 0) {
    titleEl.textContent = 'Massenversand läuft…';
    sumEl.innerHTML = `<strong>${ok}</strong> gesendet · <strong>${err}</strong> Fehler · <strong>${pending}</strong> ausstehend (insgesamt ${total})`;
  } else {
    titleEl.textContent = err === 0
      ? 'Massenversand abgeschlossen'
      : `Massenversand mit ${err} Fehler${err === 1 ? '' : 'n'} beendet`;
    sumEl.innerHTML = `<strong>${ok}</strong> gesendet · <strong>${err}</strong> Fehler (insgesamt ${total})`;
  }
}

function _openBulkStatusModal() {
  const panel = document.getElementById('bulk-status-panel');
  panel.style.display = 'flex';
  panel.classList.remove('minimized');
  document.getElementById('bulk-status-close').style.display = 'none';
  document.getElementById('bulk-status-done').style.display = 'none';
  document.getElementById('bulk-status-retry').style.display = 'none';
  document.getElementById('bulk-status-copy-failed').style.display = 'none';
  const filtered = document.getElementById('bulk-status-filtered');
  if (filtered) { filtered.style.display = 'none'; filtered.innerHTML = ''; }
}

function _bulkRenderFilteredBanner(filteredOut) {
  const el = document.getElementById('bulk-status-filtered');
  if (!el) return;
  if (!filteredOut || filteredOut.length === 0) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  const bouncedN = filteredOut.filter(f => f.reason === 'bounced').length;
  const unsubN   = filteredOut.filter(f => f.reason === 'unsubscribed').length;
  const parts = [];
  if (bouncedN) parts.push(`<strong>${bouncedN}</strong> bouncte`);
  if (unsubN)   parts.push(`<strong>${unsubN}</strong> unsubscribed`);
  const emails = filteredOut.map(f => f.email).join(', ');
  el.innerHTML = `⚠ ${parts.join(' + ')} Adresse${filteredOut.length === 1 ? '' : 'n'} rausgefiltert: <span title="${_escHtml(emails)}">${_escHtml(emails.length > 80 ? emails.slice(0, 80) + '…' : emails)}</span>`;
  el.style.display = 'block';
}

function _bulkFinalize() {
  const hasErrors = Array.from(_bulkTracking.byAddr.values()).some(r => r.status === 'error');
  if (_bulkTracking.pollTimer) {
    clearInterval(_bulkTracking.pollTimer);
    _bulkTracking.pollTimer = null;
  }
  document.getElementById('bulk-status-close').style.display = '';
  document.getElementById('bulk-status-done').style.display = '';
  document.getElementById('bulk-status-retry').style.display = hasErrors ? '' : 'none';
  document.getElementById('bulk-status-copy-failed').style.display = hasErrors ? '' : 'none';
}

function _closeBulkStatus() {
  const panel = document.getElementById('bulk-status-panel');
  panel.style.display = 'none';
  panel.classList.remove('minimized');
  if (_bulkTracking && _bulkTracking.pollTimer) {
    clearInterval(_bulkTracking.pollTimer);
  }
  _bulkTracking = null;
}

function _toggleBulkStatusMinimized() {
  document.getElementById('bulk-status-panel').classList.toggle('minimized');
}

// Header-Klick und Minimize-Button klappen ein/aus; Schließen-/Aktion-Buttons stoppen Bubble.
document.getElementById('bulk-status-header').addEventListener('click', (e) => {
  if (e.target.closest('#bulk-status-close')) return;
  _toggleBulkStatusMinimized();
});
document.getElementById('bulk-status-close').addEventListener('click', (e) => {
  e.stopPropagation();
  _closeBulkStatus();
});
document.getElementById('bulk-status-done').addEventListener('click', _closeBulkStatus);

document.getElementById('bulk-status-copy-failed').addEventListener('click', async () => {
  if (!_bulkTracking) return;
  const failed = Array.from(_bulkTracking.byAddr.values())
    .filter(r => r.status === 'error').map(r => r.addr);
  if (failed.length === 0) return;
  try {
    await navigator.clipboard.writeText(failed.join('\n'));
    const btn = document.getElementById('bulk-status-copy-failed');
    const orig = btn.textContent;
    btn.textContent = 'Kopiert ✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  } catch (_) { /* Clipboard blockiert — still */ }
});

document.getElementById('bulk-status-retry').addEventListener('click', async () => {
  if (!_bulkTracking) return;
  const failed = Array.from(_bulkTracking.byAddr.values())
    .filter(r => r.status === 'error').map(r => r.addr);
  if (failed.length === 0) return;

  // Alte job_ids der fehlgeschlagenen aus Tracking entfernen (sonst kollidieren
  // ggf. verspätete SSE-Events des ersten Laufs mit dem Retry).
  for (const [jobId, addr] of Array.from(_bulkTracking.byJobId.entries())) {
    if (failed.includes(addr)) _bulkTracking.byJobId.delete(jobId);
  }

  document.getElementById('bulk-status-close').style.display = 'none';
  document.getElementById('bulk-status-done').style.display = 'none';
  document.getElementById('bulk-status-retry').style.display = 'none';
  document.getElementById('bulk-status-copy-failed').style.display = 'none';

  // Retry verwendet KEINE alten draft_id/attachment_ids — die wurden beim ersten Lauf konsumiert.
  const snapshot = { ..._bulkTracking.compose, draft_id: null, attachment_ids: [] };
  await _bulkStart(failed, snapshot);
});


// ── Compose-Anhänge ──────────────────────────────────────────
function _renderComposeAttachments() {
  const el = document.getElementById('ci-attachments');
  if (!_composeAttachments.length) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.innerHTML = _composeAttachments.map((a, i) => `
    <span class="ci-att-chip">
      <span>📎 ${escHtml(a.filename)}</span>
      <span style="color:var(--text2);font-size:12px">${formatBytes(a.size)}</span>
      <button class="ci-att-remove" data-i="${i}" title="Entfernen">×</button>
    </span>
  `).join('');
  el.style.display = 'flex';
  el.querySelectorAll('.ci-att-remove').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = +btn.dataset.i;
      const removed = _composeAttachments.splice(idx, 1)[0];
      if (removed) api.deleteUpload(removed.id).catch(() => {});
      _renderComposeAttachments();
    });
  });
}

document.getElementById('btn-attach').addEventListener('click', () => {
  document.getElementById('ci-file-input').click();
});

document.getElementById('ci-file-input').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  await _uploadFiles(files);
});

async function _uploadFiles(files) {
  const statusEl = document.getElementById('draft-status');
  for (const file of files) {
    if (file.size > 25 * 1024 * 1024) {
      statusEl.textContent = `${file.name}: zu groß (max. 25 MB)`;
      statusEl.style.color = 'var(--danger)';
      setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 3000);
      continue;
    }
    try {
      const fd = new FormData();
      fd.append('file', file);
      const result = await api.uploadAttachment(fd);
      _composeAttachments.push({ id: result.id, filename: result.filename, size: result.size });
      _renderComposeAttachments();
    } catch (err) {
      statusEl.textContent = `Upload fehlgeschlagen: ${err.message}`;
      statusEl.style.color = 'var(--danger)';
      setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 3000);
    }
  }
}

// ── Drag & Drop auf die Compose-Area ─────────────────────────
(function initComposeDragDrop() {
  const composeEl = document.getElementById('compose-mode');
  const overlayEl = document.getElementById('compose-drop-overlay');

  // Phase 1: dragenter auf compose-mode → Overlay einblenden (fängt ab hier alle Events)
  composeEl.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer.types.includes('Files')) return;
    e.preventDefault();
    composeEl.classList.add('drag-over');
  });

  // Phase 2: Overlay ist jetzt pointer-events:all und fängt alle weiteren Events ab
  overlayEl.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });

  overlayEl.addEventListener('dragleave', () => {
    composeEl.classList.remove('drag-over');
  });

  overlayEl.addEventListener('drop', async (e) => {
    e.preventDefault();
    composeEl.classList.remove('drag-over');
    const files = Array.from(e.dataTransfer.files || []);
    if (files.length) await _uploadFiles(files);
  });
})();

// ── Adressfelder mit Chip-Eingabe und Kontakt-Autocomplete ───
function makeAddressField(fieldId, inputId, suggestionsId, defaultPlaceholder) {
  const field = document.getElementById(fieldId);
  const input = document.getElementById(inputId);
  const box   = document.getElementById(suggestionsId);
  let chips   = []; // [{display}]
  let _acTimer = null;
  let _acActive = -1;
  let _acItems  = [];

  function addChip(text) {
    const display = text.trim().replace(/[,;]+$/, '');
    if (!display) return;
    if (chips.some(c => c.display === display)) return; // Duplikat
    chips.push({ display });
    const chip = document.createElement('span');
    chip.className = 'address-chip';
    chip.title = display;
    chip.innerHTML = `<span class="chip-label">${escHtml(display)}</span><button class="chip-remove" type="button">×</button>`;
    chip.querySelector('.chip-remove').addEventListener('click', () => {
      chips = chips.filter(c => c.display !== display);
      chip.remove();
      input.placeholder = chips.length ? '' : defaultPlaceholder;
    });
    field.insertBefore(chip, input);
    input.placeholder = '';
    input.value = '';
  }

  function getAddresses() {
    const result = chips.map(c => c.display);
    const typed = input.value.trim();
    if (typed) result.push(typed);
    return result;
  }

  function setAddresses(arr) {
    field.querySelectorAll('.address-chip').forEach(c => c.remove());
    chips = [];
    (arr || []).filter(Boolean).forEach(a => addChip(a));
    input.value = '';
    input.placeholder = chips.length ? '' : defaultPlaceholder;
  }

  function clear() { setAddresses([]); }

  // Autocomplete
  function renderSuggestions(contacts) {
    _acItems = contacts;
    _acActive = 0;
    if (!contacts.length) { box.classList.remove('open'); return; }
    box.innerHTML = contacts.map((c, i) =>
      `<div class="contact-suggestion${i === 0 ? ' active' : ''}" data-i="${i}">
        ${c.name ? `<span class="cs-name">${escHtml(c.name)}</span>` : ''}
        <span class="cs-email">${escHtml(c.email)}</span>
      </div>`
    ).join('');
    box.querySelectorAll('.contact-suggestion').forEach(row => {
      row.addEventListener('mouseover', () => {
        _acActive = +row.dataset.i;
        box.querySelectorAll('.contact-suggestion').forEach((r, i) =>
          r.classList.toggle('active', i === _acActive));
      });
    });
    box.classList.add('open');
  }

  function closeSuggestions() { box.classList.remove('open'); }

  function pickContact(contact) {
    const display = contact.name
      ? `${contact.name} <${contact.email}>`
      : contact.email;
    addChip(display);
    closeSuggestions();
    _acItems = [];
  }

  box.addEventListener('mousedown', e => {
    const row = e.target.closest('.contact-suggestion');
    if (row) { e.preventDefault(); pickContact(_acItems[+row.dataset.i]); }
  });

  field.addEventListener('click', () => input.focus());

  input.addEventListener('input', () => {
    clearTimeout(_acTimer);
    const q = input.value.trim();
    if (q.length < 1) { closeSuggestions(); return; }
    _acTimer = setTimeout(async () => {
      try {
        const data = await api.searchContacts(q);
        renderSuggestions(data.items || []);
      } catch (_) { closeSuggestions(); }
    }, 200);
  });

  input.addEventListener('keydown', e => {
    if (e.key === ',' || e.key === ';') {
      e.preventDefault();
      const v = input.value.trim();
      if (v) addChip(v);
      closeSuggestions();
      return;
    }
    if (e.key === 'Backspace' && input.value === '' && chips.length > 0) {
      const lastChip = field.querySelector('.address-chip:last-of-type');
      if (lastChip) { lastChip.remove(); chips.pop(); }
      if (!chips.length) input.placeholder = defaultPlaceholder;
      return;
    }
    if (!box.classList.contains('open')) return;
    const rows = box.querySelectorAll('.contact-suggestion');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _acActive = Math.min(_acActive + 1, rows.length - 1);
      rows.forEach((r, i) => r.classList.toggle('active', i === _acActive));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _acActive = Math.max(_acActive - 1, 0);
      rows.forEach((r, i) => r.classList.toggle('active', i === _acActive));
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      if (_acActive >= 0 && _acItems[_acActive]) {
        e.preventDefault();
        pickContact(_acItems[_acActive]);
      } else if (e.key === 'Tab') {
        const v = input.value.trim();
        if (v) { e.preventDefault(); addChip(v); }
        closeSuggestions();
      } else {
        closeSuggestions();
      }
    } else if (e.key === 'Escape') {
      closeSuggestions();
    }
  });

  function commitPending() {
    closeSuggestions();
    // Beim Verlassen/Tabben des Feldes: getippten Text noch als Chip hinzufügen.
    const v = input.value.trim();
    if (v) {
      addChip(v);
      return true;
    }
    return false;
  }

  input.addEventListener('blur', () => {
    setTimeout(() => {
      commitPending();
    }, 150);
  });

  return { getAddresses, setAddresses, clear, commitPending };
}

const _toField = makeAddressField('ci-to-field', 'ci-to-input', 'ci-to-suggestions', 'empfaenger@beispiel.de');
const _ccField = makeAddressField('ci-cc-field', 'ci-cc-input', 'ci-cc-suggestions', '');
