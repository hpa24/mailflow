// Kleine Dropdown-Menues fuer die festen Toolbars (Topbar + Detail).
// Popover werden fixed positioniert, damit sie nicht von overflow:hidden
// in #view-mode/#detail-panel abgeschnitten werden.

(function () {
  const OPEN_CLASS = 'open';

  function closeAll(except) {
    document.querySelectorAll('.toolbar-menu.open').forEach(menu => {
      if (menu === except) return;
      menu.classList.remove(OPEN_CLASS);
      const btn = menu.querySelector('.action-btn[aria-expanded]');
      if (btn) btn.setAttribute('aria-expanded', 'false');
    });
  }

  function positionPopover(menu) {
    const trigger = menu.querySelector('.action-btn');
    const popover = menu.querySelector('.toolbar-menu-popover');
    if (!trigger || !popover) return;

    const rect = trigger.getBoundingClientRect();
    const width = Math.max(popover.offsetWidth || 190, rect.width);
    const left = Math.max(8, Math.min(window.innerWidth - width - 8, rect.right - width));
    const top = Math.min(window.innerHeight - 8, rect.bottom + 6);

    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
    popover.style.minWidth = `${width}px`;
  }

  function toggleMenu(menu) {
    const willOpen = !menu.classList.contains(OPEN_CLASS);
    closeAll(menu);
    menu.classList.toggle(OPEN_CLASS, willOpen);
    const btn = menu.querySelector('.action-btn[aria-expanded]');
    if (btn) btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    if (willOpen) positionPopover(menu);
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.toolbar-menu').forEach(menu => {
      const trigger = menu.querySelector('.action-btn');
      if (!trigger) return;
      trigger.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleMenu(menu);
      });
      menu.querySelectorAll('.menu-action-btn').forEach(btn => {
        btn.addEventListener('click', () => closeAll());
      });
    });
  });

  document.addEventListener('click', () => closeAll());
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeAll();
  });
  window.addEventListener('resize', () => closeAll());
  window.addEventListener('scroll', () => closeAll(), true);

  window.mfToolbarMenus = { closeAll, positionPopover };
})();
