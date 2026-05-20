// Webhooks-Verwaltung — ausgegliedert aus inbox.js (C4 Phase 1).
// Eigenes Modal, eigener State. Nutzt window.api (global), kein inbox.js-Zugriff.

const _webhooks = {
  modal: null, listView: null, editView: null, logsView: null,
  list: [], smtpServers: [], accounts: [], currentId: null,
  initialized: false,
};

function _whEl(id) { return document.getElementById(id); }

async function openWebhooksModal() {
  const ov = _whEl('webhooks-modal-overlay');
  ov.style.display = 'flex';
  if (!_webhooks.initialized) _initWebhooksHandlers();
  _showWebhookListView();
  await _loadWebhooks();
}

function closeWebhooksModal() {
  _whEl('webhooks-modal-overlay').style.display = 'none';
  _setWhStatus('');
}

function _setWhStatus(text, isError = false) {
  const el = _whEl('webhooks-modal-status');
  el.textContent = text || '';
  el.style.color = isError ? '#c00' : '';
}

function _showWebhookListView() {
  _whEl('webhooks-list-view').style.display = '';
  _whEl('webhook-edit-view').style.display = 'none';
  _whEl('webhook-logs-view').style.display = 'none';
  _whEl('webhooks-modal-cancel').style.display = 'none';
  _whEl('webhooks-modal-delete').style.display = 'none';
  _whEl('webhooks-modal-save').style.display = 'none';
  _whEl('webhooks-modal-done').style.display = '';
  _setWhStatus('');
}

function _showWebhookEditView(isNew) {
  _whEl('webhooks-list-view').style.display = 'none';
  _whEl('webhook-edit-view').style.display = '';
  _whEl('webhook-logs-view').style.display = 'none';
  _whEl('webhooks-modal-cancel').style.display = '';
  _whEl('webhooks-modal-delete').style.display = isNew ? 'none' : '';
  _whEl('webhooks-modal-save').style.display = '';
  _whEl('webhooks-modal-done').style.display = 'none';
  _whEl('wh-credentials-row').style.display = isNew ? 'none' : '';
  _whEl('wh-apikey-row').style.display = isNew ? 'none' : '';
  _setWhStatus('');
}

function _showWebhookLogsView() {
  _whEl('webhooks-list-view').style.display = 'none';
  _whEl('webhook-edit-view').style.display = 'none';
  _whEl('webhook-logs-view').style.display = '';
  _whEl('webhooks-modal-cancel').style.display = 'none';
  _whEl('webhooks-modal-delete').style.display = 'none';
  _whEl('webhooks-modal-save').style.display = 'none';
  _whEl('webhooks-modal-done').style.display = '';
  _setWhStatus('');
}

async function _loadWebhooks() {
  const listEl = _whEl('webhooks-list');
  listEl.innerHTML = '<div class="webhooks-loading">Lade…</div>';
  try {
    const [whs, smtp, accs] = await Promise.all([
      api.webhooks.list(),
      api.getSmtpServers(),
      api.getAccounts(),
    ]);
    _webhooks.list = whs || [];
    _webhooks.smtpServers = smtp?.items || (Array.isArray(smtp) ? smtp : []);
    _webhooks.accounts = accs?.items || (Array.isArray(accs) ? accs : []);
    _renderWebhooksList();
  } catch (e) {
    listEl.innerHTML = `<div class="webhooks-error">Fehler: ${e.message}</div>`;
  }
}

function _renderWebhooksList() {
  const listEl = _whEl('webhooks-list');
  if (!_webhooks.list.length) {
    listEl.innerHTML = '<div class="webhooks-empty">Noch keine Webhooks angelegt.</div>';
    return;
  }
  const smtpById = Object.fromEntries(_webhooks.smtpServers.map(s => [s.id, s]));
  const accById  = Object.fromEntries(_webhooks.accounts.map(a => [a.id, a]));
  listEl.innerHTML = '';
  _webhooks.list.forEach(wh => {
    const row = document.createElement('div');
    row.className = 'webhooks-row' + (wh.is_active ? '' : ' inactive');
    const smtpName = smtpById[wh.smtp_server]?.name || '–';
    const accName  = accById[wh.from_account]?.name || accById[wh.from_account]?.from_email || '–';
    row.innerHTML = `
      <div class="wh-row-main">
        <div class="wh-row-name">${_escapeHtml(wh.name)}${wh.is_active ? '' : ' <span class="wh-badge-paused">pausiert</span>'}</div>
        <div class="wh-row-meta">
          <code>${_escapeHtml(wh.slug)}</code> · SMTP: ${_escapeHtml(smtpName)} · Account: ${_escapeHtml(accName)}
        </div>
      </div>
      <div class="wh-row-actions">
        <button class="action-btn" data-act="logs">Logs</button>
        <button class="action-btn" data-act="edit">Bearbeiten</button>
      </div>`;
    row.querySelector('[data-act="logs"]').addEventListener('click', () => _openWebhookLogs(wh));
    row.querySelector('[data-act="edit"]').addEventListener('click', () => _openWebhookEdit(wh));
    listEl.appendChild(row);
  });
}

function _fillSmtpDropdown(selectedId) {
  const sel = _whEl('wh-smtp-server');
  sel.innerHTML = '<option value="">— wählen —</option>';
  _webhooks.smtpServers.forEach(s => {
    const o = document.createElement('option');
    o.value = s.id;
    o.textContent = s.name || `${s.host}:${s.port}`;
    if (s.id === selectedId) o.selected = true;
    sel.appendChild(o);
  });
}

function _fillAccountDropdown(selectedId) {
  const sel = _whEl('wh-from-account');
  sel.innerHTML = '<option value="">— wählen —</option>';
  _webhooks.accounts.forEach(a => {
    const o = document.createElement('option');
    o.value = a.id;
    o.textContent = a.name || a.from_email || a.id;
    if (a.id === selectedId) o.selected = true;
    sel.appendChild(o);
  });
}

function _openWebhookNew() {
  _webhooks.currentId = null;
  _whEl('wh-name').value = '';
  _whEl('wh-slug').value = '';
  _whEl('wh-from-name-override').value = '';
  _whEl('wh-default-to').value = '';
  _whEl('wh-allow-to-override').checked = true;
  _whEl('wh-allow-reply-to').checked = true;
  _whEl('wh-allow-cc').checked = false;
  _whEl('wh-is-active').checked = true;
  _fillSmtpDropdown('');
  _fillAccountDropdown('');
  _showWebhookEditView(true);
}

function _openWebhookEdit(wh) {
  _webhooks.currentId = wh.id;
  _whEl('wh-name').value = wh.name || '';
  _whEl('wh-slug').value = wh.slug || '';
  _whEl('wh-from-name-override').value = wh.from_name_override || '';
  _whEl('wh-default-to').value = wh.default_to || '';
  _whEl('wh-allow-to-override').checked = !!wh.allow_to_override;
  _whEl('wh-allow-reply-to').checked = !!wh.allow_reply_to;
  _whEl('wh-allow-cc').checked = !!wh.allow_cc;
  _whEl('wh-is-active').checked = !!wh.is_active;
  _fillSmtpDropdown(wh.smtp_server);
  _fillAccountDropdown(wh.from_account);
  _whEl('wh-url').value = api.webhooks.sendUrl(wh.slug);
  _whEl('wh-apikey').value = wh.api_key || '';
  _showWebhookEditView(false);
}

async function _saveWebhook() {
  const body = {
    name: _whEl('wh-name').value.trim(),
    slug: _whEl('wh-slug').value.trim().toLowerCase(),
    smtp_server: _whEl('wh-smtp-server').value,
    from_account: _whEl('wh-from-account').value,
    from_name_override: _whEl('wh-from-name-override').value.trim(),
    default_to: _whEl('wh-default-to').value.trim(),
    allow_to_override: _whEl('wh-allow-to-override').checked,
    allow_reply_to: _whEl('wh-allow-reply-to').checked,
    allow_cc: _whEl('wh-allow-cc').checked,
    is_active: _whEl('wh-is-active').checked,
  };
  if (!body.name) { _setWhStatus('Name fehlt', true); return; }
  if (!/^[a-z0-9-]+$/.test(body.slug)) { _setWhStatus('Slug ungültig (nur a-z, 0-9, -)', true); return; }
  if (!body.smtp_server) { _setWhStatus('SMTP-Server wählen', true); return; }
  if (!body.from_account) { _setWhStatus('Absender-Account wählen', true); return; }
  _setWhStatus('Speichere…');
  try {
    let saved;
    if (_webhooks.currentId) {
      saved = await api.webhooks.update(_webhooks.currentId, body);
    } else {
      saved = await api.webhooks.create(body);
    }
    _setWhStatus('Gespeichert');
    await _loadWebhooks();
    _openWebhookEdit(saved);
  } catch (e) {
    _setWhStatus('Fehler: ' + e.message, true);
  }
}

async function _deleteWebhook() {
  if (!_webhooks.currentId) return;
  if (!confirm('Diesen Webhook wirklich löschen? Alle zugehörigen Logs gehen verloren.')) return;
  _setWhStatus('Lösche…');
  try {
    await api.webhooks.delete(_webhooks.currentId);
    _webhooks.currentId = null;
    _showWebhookListView();
    await _loadWebhooks();
  } catch (e) {
    _setWhStatus('Fehler: ' + e.message, true);
  }
}

async function _rotateApiKey() {
  if (!_webhooks.currentId) return;
  if (!confirm('Neuen API-Key erzeugen? Der alte funktioniert dann nicht mehr — Xano-Konfig anpassen!')) return;
  _setWhStatus('Erzeuge neuen Key…');
  try {
    const saved = await api.webhooks.update(_webhooks.currentId, { rotate_api_key: true });
    _whEl('wh-apikey').value = saved.api_key;
    _setWhStatus('Neuer Key erzeugt');
  } catch (e) {
    _setWhStatus('Fehler: ' + e.message, true);
  }
}

async function _openWebhookLogs(wh) {
  _webhooks.currentId = wh.id;
  _whEl('webhook-logs-title').textContent = `Logs: ${wh.name}`;
  _showWebhookLogsView();
  await _loadWebhookLogs();
}

async function _loadWebhookLogs() {
  const listEl = _whEl('webhook-logs-list');
  listEl.innerHTML = '<div class="webhooks-loading">Lade…</div>';
  try {
    const logs = await api.webhooks.logs(_webhooks.currentId, 100);
    if (!logs.length) {
      listEl.innerHTML = '<div class="webhooks-empty">Keine Logs vorhanden.</div>';
      return;
    }
    listEl.innerHTML = '';
    logs.forEach(l => {
      const row = document.createElement('div');
      row.className = 'wh-log-row ' + (l.status === 'success' ? 'ok' : 'err');
      const dt = new Date(l.created).toLocaleString('de-DE');
      row.innerHTML = `
        <div class="wh-log-head">
          <span class="wh-log-status">${l.status === 'success' ? '✓' : '✗'}</span>
          <span class="wh-log-dt">${dt}</span>
          <span class="wh-log-ip">${_escapeHtml(l.ip || '')}</span>
        </div>
        <div class="wh-log-body">
          <div><strong>An:</strong> ${_escapeHtml(l.to || '')}</div>
          <div><strong>Betreff:</strong> ${_escapeHtml(l.subject || '')}</div>
          ${l.error ? `<div class="wh-log-err"><strong>Fehler:</strong> ${_escapeHtml(l.error)}</div>` : ''}
          ${l.message_id ? `<div class="wh-log-mid"><strong>Message-ID:</strong> <code>${_escapeHtml(l.message_id)}</code></div>` : ''}
        </div>`;
      listEl.appendChild(row);
    });
  } catch (e) {
    listEl.innerHTML = `<div class="webhooks-error">Fehler: ${e.message}</div>`;
  }
}

function _copyToClipboard(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✓';
      setTimeout(() => { btn.textContent = orig; }, 1200);
    }
  });
}

function _escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function _initWebhooksHandlers() {
  _webhooks.initialized = true;
  _whEl('webhooks-modal-close').addEventListener('click', closeWebhooksModal);
  _whEl('webhooks-modal-done').addEventListener('click', closeWebhooksModal);
  _whEl('btn-webhook-new').addEventListener('click', _openWebhookNew);
  _whEl('webhooks-modal-save').addEventListener('click', _saveWebhook);
  _whEl('webhooks-modal-delete').addEventListener('click', _deleteWebhook);
  _whEl('webhooks-modal-cancel').addEventListener('click', () => {
    _showWebhookListView();
    _loadWebhooks();
  });
  _whEl('btn-webhook-logs-back').addEventListener('click', () => _showWebhookListView());
  _whEl('btn-webhook-logs-refresh').addEventListener('click', _loadWebhookLogs);
  _whEl('wh-url-copy').addEventListener('click', e => _copyToClipboard(_whEl('wh-url').value, e.target));
  _whEl('wh-apikey-copy').addEventListener('click', e => _copyToClipboard(_whEl('wh-apikey').value, e.target));
  _whEl('wh-apikey-rotate').addEventListener('click', _rotateApiKey);
  // Slug aus Name vorbefüllen (nur wenn leer)
  _whEl('wh-name').addEventListener('input', () => {
    const slugInput = _whEl('wh-slug');
    if (!slugInput.dataset.touched) {
      slugInput.value = _whEl('wh-name').value
        .toLowerCase()
        .replace(/[äöüß]/g, c => ({ä:'ae',ö:'oe',ü:'ue',ß:'ss'}[c]))
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 100);
    }
  });
  _whEl('wh-slug').addEventListener('input', e => { e.target.dataset.touched = '1'; });
}

// Topbar-Button-Hook
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('btn-webhooks');
  if (btn) btn.addEventListener('click', openWebhooksModal);
});
