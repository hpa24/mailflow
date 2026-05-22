// Server-Sent Events — Realtime-Push für neue Mails + Versand-Ergebnisse.
//
// Ausgegliedert aus inbox.js im Rahmen von C4 Phase 2. Greift auf globale
// Helper aus inbox.js zu (`silentRefresh`, `_handleSendResult`,
// `scheduleSentTodayRefresh`) — Auflösung geschieht zur Laufzeit, wenn
// das erste Event eintrifft (zu dem Zeitpunkt ist inbox.js längst geladen).

function startEventSource() {
  let es = null;

  async function connect() {
    let url;
    try {
      url = await apiEventSourceUrl();
    } catch (_) {
      setTimeout(connect, 10_000);
      return;
    }
    es = new EventSource(url);

    es.onmessage = async (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'new-mail') {
          await silentRefresh();
        } else if (data.type === 'send-result') {
          _handleSendResult(data);
          if (data.success) scheduleSentTodayRefresh();
        }
      } catch (_) {}
    };

    es.onerror = () => {
      es.close();
      setTimeout(connect, 10_000);
    };
  }

  connect();
  window.addEventListener('beforeunload', () => { if (es) es.close(); }, { once: true });
}
