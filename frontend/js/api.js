const API = 'https://mailflow-api.barres.de';

let API_KEY = '';
let _apiKeyPromise = null;

async function _loadApiKey() {
  const authData = (typeof auth !== 'undefined') ? auth.getAuth() : null;
  const pbToken = authData?.token;
  if (!pbToken) return;
  try {
    const res = await fetch(`${API}/config.js`, {
      headers: { 'Authorization': `Bearer ${pbToken}` },
    });
    if (res.status === 401 || res.status === 403) {
      auth.logout();
      return;
    }
    if (res.ok) {
      const text = await res.text();
      const m = text.match(/MAILFLOW_API_KEY='([^']*)'/);
      if (m && m[1]) API_KEY = m[1];
    }
  } catch (_) {}
}

function _ensureApiKey() {
  if (!_apiKeyPromise) _apiKeyPromise = _loadApiKey();
  return _apiKeyPromise;
}

async function apiFetch(path, options = {}) {
  await _ensureApiKey();
  if (API_KEY) {
    options.headers = { 'X-API-Key': API_KEY, ...(options.headers || {}) };
  }
  const res = await fetch(API + path, options);
  if (res.status === 401 || res.status === 403) {
    auth.logout();
    throw new Error('Nicht autorisiert');
  }
  if (!res.ok) {
    let detail = `${res.status}`;
    try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

// GET mit Query-Parametern
function apiGet(path, params = {}) {
  const q = new URLSearchParams(params).toString();
  return apiFetch(q ? `${path}?${q}` : path);
}

// EventSource-URL mit Key als Query-Parameter (Browser-API erlaubt keine Custom-Header)
function apiEventSourceUrl() {
  const base = `${API}/events`;
  return API_KEY ? `${base}?key=${encodeURIComponent(API_KEY)}` : base;
}

// POST/PATCH mit JSON-Body
function apiJson(path, method, data) {
  return apiFetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

window.api = {
  getEmails(params = {})         { return apiGet('/emails', params); },
  getThreadedEmails(params = {}) { return apiGet('/emails/threaded', params); },
  getEmailsBySender(params = {}) { return apiGet('/emails/by-sender', params); },
  search(params = {})            { return apiGet('/search', params); },
  searchContacts(q)              { return apiGet('/contacts/search', { q }); },
  getFolderCounts()              { return apiGet('/folders/counts'); },
  getFolders(accountId)          { return apiGet('/folders', { account: accountId }); },
  getCategories()                { return apiGet('/categories'); },
  getAccounts()                  { return apiFetch('/accounts'); },
  getSmtpServers()               { return apiFetch('/smtp-servers'); },
  getSyncStatus()                { return apiFetch('/sync/status'); },
  getEmail(id)                   { return apiFetch(`/emails/${id}`); },

  moveEmail(id, targetFolder) { return apiJson(`/emails/${id}/move`, 'POST', { target_folder: targetFolder }); },
  deleteEmail(id)   { return apiFetch(`/emails/${id}`, { method: 'DELETE' }); },
  spamEmail(id, opts = {}) {
    const params = [];
    if (opts.blockSender) params.push('block_sender=true');
    if (opts.blockDomain) params.push('block_domain=true');
    const qs = params.length ? `?${params.join('&')}` : '';
    return apiFetch(`/emails/${id}/spam${qs}`, { method: 'POST' });
  },
  unspamEmail(id)             { return apiFetch(`/emails/${id}/unspam`, { method: 'POST' }); },
  spamSuggestionConfirm(id)   { return apiFetch(`/emails/${id}/spam-suggestion/confirm`, { method: 'POST' }); },
  spamSuggestionDismiss(id)   { return apiFetch(`/emails/${id}/spam-suggestion/dismiss`, { method: 'POST' }); },
  spamRulesList(account = null) {
    return account ? apiGet('/spam-rules', { account }) : apiFetch('/spam-rules');
  },
  spamRulesDelete(ruleId)     { return apiFetch(`/spam-rules/${ruleId}`, { method: 'DELETE' }); },
  syncRun()         { return apiFetch('/sync/run', { method: 'POST' }); },
  syncDraft(id)     { return apiFetch(`/emails/draft/${id}/sync`, { method: 'POST' }); },
  markRead(id)        { return apiFetch(`/emails/${id}/read?is_read=true`, { method: 'PATCH' }); },
  markUnread(id)      { return apiFetch(`/emails/${id}/read?is_read=false`, { method: 'PATCH' }); },
  bulkMarkRead(emailRefs, isRead) { return apiJson('/emails/bulk/read', 'PATCH', { emails: emailRefs, is_read: isRead }); },
  setCategory(id, cat){ return apiJson(`/emails/${id}/category`, 'PATCH', { ai_category: cat }); },

  sendEmail(data)           { return apiJson('/emails/send', 'POST', data); },
  bulkSendEmail(data)       { return apiJson('/emails/bulk-send', 'POST', data); },
  saveTriageExample(emailId, category) { return apiJson('/triage/example', 'POST', {email_id: emailId, category}); },
  updateAccount(id, data)   { return apiJson(`/accounts/${id}`, 'PATCH', data); },

  saveDraft(data) {
    const { id, ...payload } = data;
    return id
      ? apiJson(`/emails/draft/${id}`, 'PATCH', payload)
      : apiJson('/emails/draft', 'POST', payload);
  },

  getAttachments(emailId)        { return apiGet(`/emails/${emailId}/attachments`); },
  attachmentDownloadUrl(id)      { return `${API}/attachments/${id}/download?key=${API_KEY}`; },
  inlineImageUrl(emailId, cid)   { return `${API}/emails/${emailId}/inline?cid=${encodeURIComponent(cid)}&key=${API_KEY}`; },
  uploadAttachment(formData)     { return apiFetch('/attachments/upload', { method: 'POST', body: formData }); },
  deleteUpload(tempId)           { return apiFetch(`/attachments/upload/${tempId}`, { method: 'DELETE' }); },

  xano: {
    userInfo(email) { return apiGet('/xano/user-info', { email }); },
  },

  responsePatterns: {
    save(data) { return apiJson('/response-patterns', 'POST', data); },
  },

  webhooks: {
    list()                  { return apiFetch('/webhooks'); },
    create(data)            { return apiJson('/webhooks', 'POST', data); },
    update(id, data)        { return apiJson(`/webhooks/${id}`, 'PATCH', data); },
    delete(id)              { return apiFetch(`/webhooks/${id}`, { method: 'DELETE' }); },
    logs(id, limit = 100)   { return apiGet(`/webhooks/${id}/logs`, { limit }); },
    sendUrl(slug)           { return `${API}/webhooks/${encodeURIComponent(slug)}/send`; },
  },

  variables: {
    list()                  { return apiFetch('/variables'); },
    create(data)            { return apiJson('/variables', 'POST', data); },
    update(id, data)        { return apiJson(`/variables/${id}`, 'PATCH', data); },
    delete(id)              { return apiFetch(`/variables/${id}`, { method: 'DELETE' }); },
  },

  snippets: {
    list()                  { return apiFetch('/snippets'); },
    create(data)            { return apiJson('/snippets', 'POST', data); },
    update(id, data)        { return apiJson(`/snippets/${id}`, 'PATCH', data); },
    delete(id)              { return apiFetch(`/snippets/${id}`, { method: 'DELETE' }); },
  },

  templates: {
    list(params = {})       { return apiGet('/templates', params); },
    create(data)            { return apiJson('/templates', 'POST', data); },
    update(id, data)        { return apiJson(`/templates/${id}`, 'PATCH', data); },
    delete(id)              { return apiFetch(`/templates/${id}`, { method: 'DELETE' }); },
  },

  ai: {
    triage(accountId, folder) {
      const body = {};
      if (accountId) body.account_id = accountId;
      if (folder)    body.folder     = folder;
      return apiJson('/ai/triage', 'POST', body);
    },
    suggest(emailId, tone = 'neutral', contextElements = null) {
      const body = { email_id: emailId, tone };
      if (contextElements && contextElements.length) body.context_elements = contextElements;
      return apiJson('/ai/suggest', 'POST', body);
    },
    refine(text, instruction) {
      return apiJson('/ai/refine', 'POST', { text, instruction });
    },
    analyze(emailId) {
      return apiJson('/ai/analyze', 'POST', { email_id: emailId });
    },
  },
};
