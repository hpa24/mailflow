const PB_URL = window.location.hostname === 'mailflow.barres.de'
  ? 'https://mailflow-pb.barres.de'
  : 'http://localhost:8090';
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

window.auth = { requireAuth, logout, login, isLoggedIn, getAuth };
