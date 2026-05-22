// Detail-Ansicht einer einzelnen E-Mail — Header, Meta, Body (HTML im
// sandbox-Iframe oder Plaintext), Action-Buttons (Reply/Forward/Edit-Draft/
// Sync-Draft/Spam/Delete/Read-Toggle/KI-Suggest), Anhänge.
//
// Ausgegliedert aus inbox.js im Rahmen von C4 Phase 2. Greift wie die anderen
// extrahierten Module über das shared script-scope auf inbox.js-Globals zu
// (state, DEFAULT_ZOOM, _iframeZoom, _activeIframe*, _applyZoom, _withZoom,
// _editingDraftItemEl, _updateDetailKiBar, loadAttachments, openCompose,
// openKiAnalyzeSidebar, closeKiAnalyzeSidebar, _runKiSuggest, deleteEmail,
// _adjustFolderCount, showTab, escHtml).

async function openEmail(email, itemEl) {
  state.activeEmailId = email.id;
  document.querySelectorAll('.email-item').forEach(el => el.classList.remove('active'));
  itemEl.classList.add('active');

  // Wenn Compose offen ist: zum E-Mail-Tab wechseln (Compose bleibt erhalten)
  if (document.getElementById('detail-tabs').style.display !== 'none') {
    showTab('email');
  }

  const header = document.getElementById('detail-header');
  const body = document.getElementById('detail-body');
  const empty = document.getElementById('detail-empty');
  const actions = document.getElementById('detail-actions');

  // Zoom-State für neue E-Mail zurücksetzen (Default 125%)
  _activeIframe = null;
  _activeIframeBaseHtml = null;
  _iframeZoom = DEFAULT_ZOOM;
  body.style.zoom = '';
  _applyZoom();

  empty.style.display = 'none';
  header.style.display = 'block';
  actions.style.display = 'flex';
  body.style.display = 'block';
  document.getElementById('detail-attachments').style.display = 'none';

  // KI-Kategorie-Bar: nur im KI-Modus anzeigen
  const kiBar = document.getElementById('detail-ki-bar');
  kiBar.style.display = state.kiModeActive ? 'flex' : 'none';
  if (state.kiModeActive) {
    _updateDetailKiBar(email.ai_category || '');
  }

  document.getElementById('detail-subject').textContent = email.subject || '(kein Betreff)';
  const _renderDetailMeta = (e) => {
    const ccList = (e.cc_emails || []).filter(Boolean);
    document.getElementById('detail-meta').innerHTML = `
      <span style="width:60px;display:inline-block;">Von:</span> ${escHtml(e.from_name ? `${e.from_name} <${e.from_email}>` : e.from_email)}<br>
      <span style="width:60px;display:inline-block;">An:</span> ${escHtml((e.to_emails || []).join(', '))}<br>
      ${ccList.length ? `<span style="width:60px;display:inline-block;">Cc:</span> ${escHtml(ccList.join(', '))}<br>` : ''}
      <span style="width:60px;display:inline-block;">Datum:</span> ${e.date_sent ? new Date(e.date_sent).toLocaleString('de-DE') : '–'}
      ${e.is_answered ? '<br><span style="color:var(--accent);font-size:12px">↩ Beantwortet</span>' : ''}
    `;
  };
  _renderDetailMeta(email);
  body.textContent = 'Lade…';
  document.getElementById('btn-reply').onclick = null;
  document.getElementById('btn-forward').onclick = null;
  document.getElementById('btn-edit-draft').onclick = null;

  const isDraft = (email.folder || '').toLowerCase().includes('draft');

  // Buttons je nach Ordner ein-/ausblenden
  document.getElementById('btn-edit-draft').style.display  = isDraft ? '' : 'none';
  document.getElementById('btn-send-draft').style.display  = isDraft ? '' : 'none';
  document.getElementById('btn-sync-draft').style.display  = isDraft ? '' : 'none';
  document.getElementById('btn-reply').style.display       = isDraft ? 'none' : '';
  document.getElementById('btn-forward').style.display     = isDraft ? 'none' : '';
  document.getElementById('btn-toggle-read').style.display = isDraft ? 'none' : '';
  const _inSpam = email.folder === 'Spam';
  document.getElementById('btn-spam').style.display        = (isDraft || _inSpam) ? 'none' : '';
  document.getElementById('btn-spam-block').style.display  = (isDraft || _inSpam) ? 'none' : '';
  const detailMoreMenu = document.getElementById('detail-more-menu');
  if (detailMoreMenu) {
    const hasVisibleMenuItems = ['btn-forward', 'btn-toggle-read', 'btn-spam', 'btn-spam-block']
      .some(id => document.getElementById(id)?.style.display !== 'none');
    detailMoreMenu.style.display = hasVisibleMenuItems ? '' : 'none';
  }

  // KI-Suggest-Button: nur anzeigen wenn KI-Modus aktiv und kein Draft
  const kiSuggestBtn = document.getElementById('btn-ki-suggest');
  kiSuggestBtn.style.display = (state.kiModeActive && !isDraft) ? '' : 'none';
  kiSuggestBtn.onclick = null;

  // KI-Analyse-Button: nur anzeigen wenn KI-Modus aktiv und kein Draft
  const kiAnalyzeBtn = document.getElementById('btn-ki-analyze');
  kiAnalyzeBtn.style.display = (state.kiModeActive && !isDraft) ? '' : 'none';
  kiAnalyzeBtn.onclick = () => openKiAnalyzeSidebar(email.id, email.from_email);

  // Sidebar schließen wenn neue E-Mail geöffnet wird
  closeKiAnalyzeSidebar();

  // Handler wird erst nach Full-Email-Load gesetzt (braucht quote-Text aus full)

  try {
    const full = await api.getEmail(email.id);
    _renderDetailMeta(full);
    // body_plain bevorzugen; Fallback: plain text aus body_html extrahieren (HTML-only-E-Mails)
    let text = full.body_plain || '';
    if (!text && full.body_html) {
      const _tmp = document.createElement('div');
      _tmp.innerHTML = full.body_html
        .replace(/<br\s*\/?>/gi, '\n')
        .replace(/<\/p>/gi, '\n\n')
        .replace(/<\/div>/gi, '\n')
        .replace(/<\/tr>/gi, '\n')
        .replace(/<\/li>/gi, '\n');
      text = (_tmp.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
    }
    if (full.body_html && full.body_html.trim()) {
      // HTML in sandboxiertem Iframe rendern.
      // allow-same-origin OHNE allow-scripts: Parent kann contentDocument
      // lesen (Höhenmessung), aber eingebettetes E-Mail-JS kann nicht laufen.
      const iframe = document.createElement('iframe');
      iframe.setAttribute('sandbox', 'allow-popups allow-popups-to-escape-sandbox allow-same-origin');
      iframe.style.cssText = 'width:100%;border:none;display:block;';

      const injectCss = `<style>img{max-width:100%!important;height:auto!important}</style>`;
      const injectBase = `<base target="_blank">`;

      let htmlToRender;
      const isFullDoc = /<html[\s>]/i.test(full.body_html);
      if (isFullDoc) {
        // Vollständiges HTML-Dokument: eigenes <head> mit charset behalten,
        // nur <base> + CID-Fix injizieren. Kein erneutes Einwickeln (verhindert
        // doppelte <html>-Tags und Charset-Durcheinander).
        let h = full.body_html;
        // Evtl. vorhandene <base>-Tags ersetzen, damit target="_blank" greift
        h = h.replace(/<base\b[^>]*>/gi, '');
        if (/<head[\s>]/i.test(h)) {
          h = h.replace(/(<head[^>]*>)/i, `$1${injectBase}${injectCss}`);
        } else {
          h = injectBase + injectCss + h;
        }
        htmlToRender = h;
      } else {
        // HTML-Fragment: in minimales Dokument einwickeln
        htmlToRender = `<!DOCTYPE html><html><head><meta charset="utf-8">${injectBase}${injectCss}
          <style>
            body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
                 font-size:14px;padding:16px;margin:0;color:#1c1c1e;word-wrap:break-word;line-height:1.5}
            a{color:#0a84ff}
            pre,code{white-space:pre-wrap;background:#f5f5f7;padding:2px 4px;border-radius:3px}
            blockquote{border-left:3px solid #ccc;margin-left:0;padding-left:12px;color:#666}
          </style></head><body>${full.body_html}</body></html>`;
      }

      // Höhe vom Parent aus messen — kein Script-Inject in fremdes Mail-HTML.
      // Nutzt contentDocument (durch allow-same-origin erlaubt). Reagiert auf
      // Initial-Load + spätere Layout-Änderungen (z.B. nachladende Bilder).
      const setHeight = () => {
        try {
          const doc = iframe.contentDocument;
          if (!doc) return;
          const h = Math.max(
            doc.documentElement?.scrollHeight || 0,
            doc.body?.scrollHeight || 0
          );
          if (h > 0) iframe.style.height = (h + 32) + 'px';
        } catch (_) { /* cross-origin oder noch nicht geladen */ }
      };
      let resizeObs = null;
      iframe.addEventListener('load', () => {
        setHeight();
        try {
          const doc = iframe.contentDocument;
          if (doc && doc.body && 'ResizeObserver' in window) {
            resizeObs = new ResizeObserver(setHeight);
            resizeObs.observe(doc.body);
          }
          // Spät ladende Bilder triggern keinen ResizeObserver-Reflow zuverlässig
          if (doc) {
            doc.querySelectorAll('img').forEach(img => {
              if (!img.complete) img.addEventListener('load', setHeight, { once: true });
            });
          }
        } catch (_) { /* same-origin-Zugriff fehlgeschlagen */ }
      });
      // ResizeObserver + Image-Listener aufräumen, wenn iframe entfernt wird
      const cleanup = new MutationObserver(() => {
        if (!document.body.contains(iframe)) {
          if (resizeObs) resizeObs.disconnect();
          cleanup.disconnect();
        }
      });
      cleanup.observe(document.body, { childList: true, subtree: true });

      // cid:-Referenzen durch Backend-Proxy ersetzen — URLs vorher signieren (parallel)
      const cids = [...new Set([...htmlToRender.matchAll(/src=["']cid:([^"']+)["']/gi)].map(m => m[1]))];
      if (cids.length) {
        const signedByCid = {};
        await Promise.all(cids.map(async (cid) => {
          try { signedByCid[cid] = await api.inlineImageUrl(full.id, cid); } catch (_) {}
        }));
        htmlToRender = htmlToRender.replace(/src=["']cid:([^"']+)["']/gi, (m, cid) =>
          signedByCid[cid] ? `src="${signedByCid[cid]}"` : m
        );
      }
      _activeIframe = iframe;
      _activeIframeBaseHtml = htmlToRender;
      iframe.srcdoc = _withZoom(htmlToRender);
      body.innerHTML = '';
      body.style.display = 'flex';
      body.appendChild(iframe);
    } else {
      _activeIframe = null;
      _activeIframeBaseHtml = null;
      body.style.zoom = _iframeZoom;
      body.innerHTML = text ? linkify(escHtml(text)) : '<em style="color:#999">Kein Inhalt</em>';
    }

    // Anhänge laden und anzeigen
    await loadAttachments(full);

    if (isDraft) {
      const draftTo      = (full.to_emails || []).join(', ');
      const draftSubject = full.subject || '';

      // "Bearbeiten" — bestehenden Draft bearbeiten, kein neuer Draft
      document.getElementById('btn-edit-draft').onclick = () => {
        _editingDraftItemEl = itemEl;
        openCompose({ to: draftTo, subject: draftSubject, body: text, existingDraftId: email.id });
      };

      // "Senden" — neue Compose mit vorausgefüllten Feldern (eigener neuer Draft)
      document.getElementById('btn-send-draft').onclick = () =>
        openCompose({ to: draftTo, subject: draftSubject, body: text });
    } else {
      // Antworten-Button
      const replyTo = full.reply_to || full.from_email || '';
      const replyToFromEmail = (full.reply_to && full.reply_to !== full.from_email) ? full.from_email : null;
      const replySubject = (full.subject || '').startsWith('Re:')
        ? full.subject : `Re: ${full.subject || ''}`;
      state._currentReplyOpts = { to: replyTo, subject: replySubject, quote: text, quoteHtml: full.body_html || '', replyToEmailId: email.id, replyToFromEmail };
      document.getElementById('btn-reply').onclick = () =>
        openCompose({ to: replyTo, subject: replySubject, quote: text, quoteHtml: full.body_html || '', replyToEmailId: email.id, replyToFromEmail });

      // Weiterleiten-Button
      const fwdSubject = (full.subject || '').startsWith('Fwd:')
        ? full.subject : `Fwd: ${full.subject || ''}`;
      document.getElementById('btn-forward').onclick = () =>
        openCompose({ to: '', subject: fwdSubject, quote: text, quoteHtml: full.body_html || '' });

      // Read-Toggle-Button aktualisieren
      updateReadToggle(email, itemEl);

      // KI-Suggest-Handler
      if (state.kiModeActive) {
        kiSuggestBtn.onclick = () => _runKiSuggest(kiSuggestBtn, []);
      }
    }

    // Synchronisieren-Button (nur Drafts)
    if (isDraft) {
      const btnSync = document.getElementById('btn-sync-draft');
      btnSync.onclick = async () => {
        const origText = btnSync.textContent;
        btnSync.disabled = true;
        btnSync.textContent = 'Synchronisiert…';
        try {
          await api.syncDraft(email.id);
          btnSync.textContent = 'Synchronisiert ✓';
          setTimeout(() => { btnSync.textContent = origText; btnSync.disabled = false; }, 2000);
        } catch (e) {
          btnSync.textContent = 'Fehler: ' + e.message;
          setTimeout(() => { btnSync.textContent = origText; btnSync.disabled = false; }, 3000);
        }
      };
    }

    // Löschen-Button (immer sichtbar)
    document.getElementById('btn-delete').onclick = () => deleteEmail(email, itemEl);
    document.getElementById('btn-spam').onclick       = () => spamEmail(email, itemEl);
    document.getElementById('btn-spam-block').onclick = () => spamEmail(email, itemEl, { blockSender: true });
  } catch (e) {
    body.textContent = 'Fehler beim Laden.';
  }
}


function updateReadToggle(email, itemEl) {
  const btn = document.getElementById('btn-toggle-read');
  btn.textContent = email.is_read ? 'Als ungelesen markieren' : 'Als gelesen markieren';
  btn.onclick = async () => {
    const newState = !email.is_read;
    email.is_read = newState;
    itemEl.classList.toggle('unread', !newState);
    updateReadToggle(email, itemEl);
    _adjustFolderCount(email.account, email.folder, newState ? -1 : +1);
    try {
      await (newState ? api.markRead(email.id) : api.markUnread(email.id));
    } catch (_) {
      email.is_read = !newState;
      itemEl.classList.toggle('unread', newState);
      updateReadToggle(email, itemEl);
      _adjustFolderCount(email.account, email.folder, newState ? +1 : -1);
    }
  };
}


function linkify(text) {
  return text.replace(
    /(https?:\/\/[^\s<>"]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
  );
}


function showEmpty() {
  document.getElementById('detail-header').style.display = 'none';
  document.getElementById('detail-actions').style.display = 'none';
  document.getElementById('detail-ki-bar').style.display = 'none';
  document.getElementById('detail-body').style.display = 'none';
  document.getElementById('detail-attachments').style.display = 'none';
  document.getElementById('detail-empty').style.display = 'flex';
}
