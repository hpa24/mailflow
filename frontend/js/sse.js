// Server-Sent Events — Realtime-Push für neue Mails + Versand-Ergebnisse.
//
// Ausgegliedert aus inbox.js im Rahmen von C4 Phase 2. Greift auf globale
// Helper aus inbox.js zu (`silentRefresh`, `_handleSendResult`,
// `scheduleSentTodayRefresh`) — Auflösung geschieht zur Laufzeit, wenn
// das erste Event eintrifft (zu dem Zeitpunkt ist inbox.js längst geladen).
//
// Halbtote Verbindungen (2026-06-12): Nach Mac-Schlaf oder Backend-Restart
// kann die TCP-Verbindung beidseitig hängen, ohne dass EventSource je ein
// onerror feuert — Events kommen dann nie wieder an („Wird gesendet…" bleibt
// stehen). Der Server schickt deshalb alle 25 s ein {"type":"ping"}-Event;
// bleibt es länger als SSE_STALE_MS aus, baut der Watchdog die Verbindung
// neu auf. Tab-Sichtbarwerden und Online-Gehen triggern die Prüfung sofort.

const SSE_STALE_MS = 65_000;  // > 2 verpasste Server-Pings (25-s-Takt)

function startEventSource() {
  let es = null;
  let lastMsgTime = Date.now();
  let reconnectTimer = null;
  let connecting = false;

  async function connect() {
    if (connecting) return;
    connecting = true;
    if (es) { es.close(); es = null; }

    let url;
    try {
      url = await apiEventSourceUrl();
    } catch (_) {
      connecting = false;
      scheduleReconnect();
      return;
    }
    lastMsgTime = Date.now();
    es = new EventSource(url);
    connecting = false;

    es.onmessage = async (e) => {
      lastMsgTime = Date.now();
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'new-mail') {
          await silentRefresh();
        } else if (data.type === 'send-result') {
          _handleSendResult(data);
          if (data.success) scheduleSentTodayRefresh();
        }
        // 'ping' und 'connected' brauchen nur das lastMsgTime-Update oben.
      } catch (_) {}
    };

    es.onerror = () => {
      if (es) { es.close(); es = null; }
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 10_000);
  }

  function reconnectIfStale() {
    if (reconnectTimer || connecting) return;
    if (Date.now() - lastMsgTime > SSE_STALE_MS) connect();
  }

  setInterval(reconnectIfStale, 15_000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) reconnectIfStale();
  });
  window.addEventListener('online', reconnectIfStale);

  connect();
  window.addEventListener('beforeunload', () => { if (es) es.close(); }, { once: true });
}
