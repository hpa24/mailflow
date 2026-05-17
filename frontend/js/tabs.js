// Topbar-Tabs: schaltet zwischen Inbox / Vorlagen / Kontakte um.
// Inbox-Elemente (#app, #ki-toolbar, #compose-mode) bleiben im DOM und werden
// per inline display-Style versteckt; ihr Original-State wird beim ersten
// Verlassen gemerkt und beim Zurueckschalten wiederhergestellt.

(function () {
  const TABS = ['inbox', 'templates', 'contacts'];
  const INBOX_ELS = ['#app', '#ki-toolbar', '#compose-mode'];
  let _inboxStates = null;

  function _saveInboxStates() {
    _inboxStates = {};
    INBOX_ELS.forEach(sel => {
      const el = document.querySelector(sel);
      if (el) _inboxStates[sel] = el.style.display || '';
    });
  }

  function _hideInbox() {
    if (_inboxStates === null) _saveInboxStates();
    INBOX_ELS.forEach(sel => {
      const el = document.querySelector(sel);
      if (el) el.style.display = 'none';
    });
  }

  function _showInbox() {
    if (_inboxStates === null) return;
    INBOX_ELS.forEach(sel => {
      const el = document.querySelector(sel);
      if (el) el.style.display = _inboxStates[sel];
    });
  }

  function setActiveTab(name) {
    if (!TABS.includes(name)) name = 'inbox';
    const prev = document.body.dataset.activeTab;
    if (prev === name) return;
    if (name === 'inbox') {
      _showInbox();
    } else {
      _hideInbox();
    }
    document.body.dataset.activeTab = name;
    document.querySelectorAll('#topbar-tabs .tab-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tab === name);
    });
    try { localStorage.setItem('mf_active_tab', name); } catch (_) {}
    window.dispatchEvent(new CustomEvent('mf:tab-changed', { detail: { tab: name } }));
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('#topbar-tabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => setActiveTab(btn.dataset.tab));
    });
    let saved = 'inbox';
    try { saved = localStorage.getItem('mf_active_tab') || 'inbox'; } catch (_) {}
    setActiveTab(saved);
  });

  window.mfTabs = { setActiveTab };
})();
