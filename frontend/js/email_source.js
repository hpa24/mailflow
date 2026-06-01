// Quelltext-Ansicht: zeigt die Roh-Mail (RFC822) einer E-Mail in einem Modal
// und bietet den .eml-Download. Der Rohtext wird live von IMAP geholt
// (api.getEmailSource) — er wird nicht in PocketBase gespeichert.

(function () {
  let _emailId = null;

  function overlay() { return document.getElementById('source-modal-overlay'); }

  function close() {
    const o = overlay();
    if (o) o.style.display = 'none';
    _emailId = null;
  }

  async function open(emailId, subject) {
    _emailId = emailId;
    const o = overlay();
    const pre = document.getElementById('source-modal-pre');
    const title = document.getElementById('source-modal-title');
    if (!o || !pre) return;
    title.textContent = subject ? `Quelltext — ${subject}` : 'Quelltext';
    pre.textContent = 'Lade…';
    o.style.display = 'flex';
    try {
      const res = await api.getEmailSource(emailId);
      pre.textContent = (res && res.source) || '(leer)';
    } catch (e) {
      pre.textContent = 'Fehler beim Laden des Quelltexts: ' + (e.message || e);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const closeBtn = document.getElementById('source-modal-close');
    const dlBtn = document.getElementById('source-modal-download');
    const o = overlay();
    if (closeBtn) closeBtn.addEventListener('click', close);
    // Klick auf den abgedunkelten Hintergrund schließt das Modal.
    if (o) o.addEventListener('click', (ev) => { if (ev.target === o) close(); });
    if (dlBtn) dlBtn.addEventListener('click', async () => {
      if (!_emailId) return;
      try {
        const url = await api.emailSourceDownloadUrl(_emailId);
        window.open(url, '_blank', 'noopener,noreferrer');
      } catch (e) {
        console.warn('source .eml sign failed:', e);
      }
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && o && o.style.display !== 'none') close();
    });
  });

  window.mfEmailSource = { open, close };
})();
