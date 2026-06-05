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

// S5 Phase 2 (2026-05-23): blockiert externe Inhalte im HTML-Body (img src,
// srcset auf img/source, inline style url(), <style>-CSS, protocol-relative).
// Signierte CID-URLs (apiOrigin) sind weißgelistet. Counter ist eine grobe
// Schätzung — img-src zählt pro Vorkommen, srcset/style/<style> jeweils einmal.
function _blockRemoteContent(html, apiOrigin) {
  let count = 0;
  const isExternal = (raw) => {
    const u = (raw || '').trim();
    if (!u) return false;
    if (u.startsWith('cid:') || u.startsWith('data:') ||
        u.startsWith('about:') || u.startsWith('mailto:') ||
        u.startsWith('#') || u.startsWith('tel:')) return false;
    if (apiOrigin && u.startsWith(apiOrigin)) return false;
    if (u.startsWith('//')) return true;            // protocol-relative
    if (/^https?:\/\//i.test(u)) return true;
    return false;                                    // relative URLs ignorieren
  };

  // <img src="..."> — transparent gif als Placeholder
  html = html.replace(
    /<img\b([^>]*?)\bsrc=(["'])([^"']+)\2([^>]*)>/gi,
    (m, before, _q, url, after) => {
      if (!isExternal(url)) return m;
      count++;
      return `<img${before}src="data:image/gif;base64,R0lGODlhAQABAAAAACw="${after}>`;
    }
  );

  // srcset auf <img>/<source> — komplettes Attribut entfernen, fällt auf src zurück
  html = html.replace(
    /\bsrcset=(["'])([^"']+)\1/gi,
    (m, _q, srcset) => {
      const candidates = srcset.split(',').map(s => s.trim()).filter(Boolean);
      const hasExternal = candidates.some(c => isExternal(c.split(/\s+/)[0]));
      if (!hasExternal) return m;
      count++;
      return '';
    }
  );

  // Inline-style mit url(...) — URLs durch about:blank ersetzen
  html = html.replace(
    /\bstyle=(["'])([^"']*url\s*\([^)]*\)[^"']*)\1/gi,
    (m, q, style) => {
      let blocked = false;
      const newStyle = style.replace(/url\(\s*(["']?)([^)"']+)\1\s*\)/gi, (mm, _qq, url) => {
        if (!isExternal(url)) return mm;
        blocked = true;
        return 'url(about:blank)';
      });
      if (!blocked) return m;
      count++;
      return `style=${q}${newStyle}${q}`;
    }
  );

  // <style>...</style> — alle url() im CSS-Block durch about:blank
  html = html.replace(/(<style\b[^>]*>)([\s\S]*?)(<\/style>)/gi, (m, open, css, close) => {
    let blocked = false;
    const newCss = css.replace(/url\(\s*(["']?)([^)"']+)\1\s*\)/gi, (mm, _q, url) => {
      if (!isExternal(url)) return mm;
      blocked = true;
      return 'url(about:blank)';
    });
    if (blocked) count++;
    return open + newCss + close;
  });

  return { html, count };
}

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
  _activeIframeOriginalHtml = null;
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
      <span style="width:60px;display:inline-block;">Von:</span><button id="btn-copy-from" class="meta-copy-btn" title="Absender-Adresse kopieren">Copy</button> ${escHtml(e.from_name ? `${e.from_name} <${e.from_email}>` : e.from_email)}<br>
      <span style="width:60px;display:inline-block;">An:</span> ${escHtml((e.to_emails || []).join(', '))}<br>
      ${ccList.length ? `<span style="width:60px;display:inline-block;">Cc:</span> ${escHtml(ccList.join(', '))}<br>` : ''}
      <span style="width:60px;display:inline-block;">Datum:</span> ${e.date_sent ? new Date(e.date_sent).toLocaleString('de-DE') : '–'}
      ${e.is_answered ? '<br><span style="color:var(--accent);font-size:12px">↩ Beantwortet</span>' : ''}
    `;
    // Copy-Button: kopiert nur die reine Absender-Adresse (from_email),
    // nie den Anzeigenamen oder die spitzen Klammern.
    const copyBtn = document.getElementById('btn-copy-from');
    if (copyBtn) copyBtn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(e.from_email || '');
        copyBtn.textContent = '✓';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1200);
      } catch (_) { /* Clipboard-Zugriff verweigert — still bleiben */ }
    };
  };
  _renderDetailMeta(email);
  body.textContent = 'Lade…';
  document.getElementById('btn-reply').onclick = null;
  document.getElementById('btn-forward').onclick = null;
  document.getElementById('btn-edit-draft').onclick = null;
  document.getElementById('btn-view-source').onclick = null;

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
  // Quelltext-Ansicht: für jede Mail mit IMAP-UID verfügbar (auch Sent/Spam).
  const viewSrcBtn = document.getElementById('btn-view-source');
  viewSrcBtn.style.display = email.imap_uid ? '' : 'none';
  viewSrcBtn.onclick = () => window.mfEmailSource.open(email.id, email.subject || '');
  const detailMoreMenu = document.getElementById('detail-more-menu');
  if (detailMoreMenu) {
    const hasVisibleMenuItems = ['btn-forward', 'btn-toggle-read', 'btn-view-source', 'btn-spam', 'btn-spam-block']
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

      // Konsole sauber halten (2026-06-05): <script>-Tags strippen. Ausführung
      // wäre durch die Sandbox (kein allow-scripts) ohnehin blockiert, erzeugt
      // aber pro Mail einen "Blocked script execution"-Konsolen-Fehler.
      htmlToRender = htmlToRender
        .replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/gi, '')
        .replace(/<script\b[^>]*\/?>/gi, '');

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

      // Nicht auflösbare cid:-Referenzen (Signierung fehlgeschlagen oder Form
      // vom Regex oben nicht erfasst) → transparenter Platzhalter. Verhindert
      // net::ERR_UNKNOWN_URL_SCHEME in der Konsole; das Bild fehlt ohnehin.
      const unresolvedCids = [...new Set([...htmlToRender.matchAll(/src=["']cid:([^"']+)["']/gi)].map(m => m[1]))];
      if (unresolvedCids.length) {
        console.warn('Mailflow: Inline-Bilder nicht auflösbar (cid):', unresolvedCids, '— E-Mail', full.id);
        htmlToRender = htmlToRender.replace(/src=(["'])cid:[^"']+\1/gi,
          'src="data:image/gif;base64,R0lGODlhAQABAAAAACw="');
      }

      // S5 Phase 2 (2026-05-23): Tracking-Schutz — neben <img src> jetzt auch
      // srcset, inline style url(), <style>-CSS und protocol-relative URLs.
      // Signierte CID-URLs (API-Origin) sind weißgelistet und durchlaufen normal.
      // Block läuft NACH CID-Replace; Original-Snapshot für vollständigen
      // Re-Render beim „Bilder laden"-Klick.
      const originalHtmlBeforeBlock = htmlToRender;
      const blockResult = _blockRemoteContent(htmlToRender, API);
      htmlToRender = blockResult.html;
      const blockedCount = blockResult.count;

      _activeIframe = iframe;
      _activeIframeBaseHtml = htmlToRender;
      _activeIframeOriginalHtml = originalHtmlBeforeBlock;
      iframe.srcdoc = _withZoom(htmlToRender);
      body.innerHTML = '';
      body.style.display = 'flex';

      // Banner für Tracking-Schutz, wenn externe Inhalte blockiert wurden
      if (blockedCount > 0) {
        const banner = document.createElement('div');
        banner.style.cssText = 'padding:8px 12px;background:#fff4e5;border:1px solid #f4c478;border-radius:6px;margin-bottom:8px;font-size:13px;display:flex;justify-content:space-between;align-items:center;gap:12px';
        // CID-Hinweis nur, wenn die Mail tatsächlich eingebettete Bilder hat
        const resolvedCidCount = cids.length - unresolvedCids.length;
        banner.innerHTML = `<span>🛡️ Externe Bilder blockiert (Tracking-Schutz).${resolvedCidCount > 0 ? ' Eingebettete Bilder bleiben sichtbar.' : ''}</span>`;
        const loadBtn = document.createElement('button');
        loadBtn.className = 'action-btn';
        loadBtn.textContent = 'Bilder laden';
        loadBtn.onclick = () => {
          // Vollständiger Re-Render aus dem unblocked-Snapshot — deckt auch
          // CSS-Hintergründe, srcset und <style>-Tags ab, die per DOM-Swap
          // nicht einzeln restaurierbar wären.
          _activeIframeBaseHtml = _activeIframeOriginalHtml;
          iframe.srcdoc = _withZoom(_activeIframeOriginalHtml);
          banner.remove();
        };
        banner.appendChild(loadBtn);
        body.appendChild(banner);
      }

      body.appendChild(iframe);
    } else {
      _activeIframe = null;
      _activeIframeBaseHtml = null;
      _activeIframeOriginalHtml = null;
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
        btnSync.textContent = 'Wird gesichert…';
        try {
          await api.syncDraft(email.id);
          btnSync.textContent = 'Liegt auf Server ✓';
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
