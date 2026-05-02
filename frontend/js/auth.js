const PB_URL = 'https://mailflow-pb.barres.de';
const AUTH_KEY = 'mf_auth';

function getAuth() {
  try {
    return JSON.parse(localStorage.getItem(AUTH_KEY) || 'null');
  } catch {
    return null;
  }
}

function setAuth(token, record) {
  localStorage.setItem(AUTH_KEY, JSON.stringify({ token, record }));
}

function clearAuth() {
  localStorage.removeItem(AUTH_KEY);
}

function isLoggedIn() {
  const auth = getAuth();
  return !!(auth && auth.token);
}

function requireAuth() {
  if (!isLoggedIn()) {
    window.location.href = '/login.html';
  }
}

function logout() {
  clearAuth();
  window.location.href = '/login.html';
}

async function authRefresh() {
  const a = getAuth();
  if (!a || !a.token) {
    logout();
    return false;
  }
  try {
    const resp = await fetch(
      `${PB_URL}/api/collections/users/auth-refresh`,
      { method: 'POST', headers: { 'Authorization': a.token } }
    );
    if (resp.status === 401 || resp.status === 403) {
      logout();
      return false;
    }
    if (resp.ok) {
      const data = await resp.json();
      setAuth(data.token, data.record);
    }
    // sonstige Fehler (Netz, 5xx) → nicht ausloggen
    return true;
  } catch {
    // Netzwerkfehler — nicht ausloggen
    return true;
  }
}

async function login(email, password) {
  const resp = await fetch(
    `${PB_URL}/api/collections/users/auth-with-password`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identity: email, password }),
    }
  );
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.message || 'Login fehlgeschlagen');
  }
  const data = await resp.json();
  setAuth(data.token, data.record);
  return data;
}

window.auth = { requireAuth, logout, login, isLoggedIn, getAuth, authRefresh };
