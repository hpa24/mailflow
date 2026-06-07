// Untermenue-Switcher im Vorlagen-Tab (Variablen / Snippets / ...).
// Setzt data-active-section auf #templates-main; CSS macht den Rest sichtbar.

(function () {
  const KNOWN = ['variables', 'snippets', 'templates', 'groups', 'bulk_sends', 'bounced'];

  function setSection(name) {
    if (!KNOWN.includes(name)) name = 'variables';
    const main = document.getElementById('templates-main');
    if (!main) return;
    const prev = main.dataset.activeSection;
    if (prev === name) return;
    main.dataset.activeSection = name;
    document.querySelectorAll('#templates-submenu .submenu-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.section === name);
    });
    try { localStorage.setItem('mf_active_section', name); } catch (_) {}
    window.dispatchEvent(new CustomEvent('mf:section-changed', { detail: { section: name } }));
  }

  // Beim Umschalten auf den Vorlagen-Tab das Section-Event fuer die aktuell
  // aktive Sektion erneut feuern, damit deren Modul seine Liste (nach)laedt —
  // sonst bleibt sie leer, weil nur mf:section-changed die Loads ausloest.
  window.addEventListener('mf:tab-changed', (e) => {
    if (e.detail.tab !== 'templates') return;
    const section = document.getElementById('templates-main')?.dataset.activeSection;
    if (!section) return;
    window.dispatchEvent(new CustomEvent('mf:section-changed', { detail: { section } }));
  });

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('#templates-submenu .submenu-btn').forEach(btn => {
      if (btn.disabled) return;
      btn.addEventListener('click', () => {
        if (btn.dataset.tabJump && window.mfTabs) {
          window.mfTabs.setActiveTab(btn.dataset.tabJump);
          return;
        }
        setSection(btn.dataset.section);
      });
    });
    let saved = 'variables';
    try { saved = localStorage.getItem('mf_active_section') || 'variables'; } catch (_) {}
    if (!KNOWN.includes(saved)) saved = 'variables';
    setSection(saved);
  });

  window.mfSubnav = { setSection };
})();
