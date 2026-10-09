// keycloak-lab page. Read top to bottom.
//
// keycloak-js is Keycloak's official browser adapter. It runs the
// "Authorization Code + PKCE" flow for us: redirect to Keycloak, come back with
// a one-time code, swap the code for tokens, refresh them before they expire.
// docs/02-login-flow.md shows each of those steps without the library.
import Keycloak from 'https://cdn.jsdelivr.net/npm/keycloak-js@26.2.4/lib/keycloak.js';

// These three values must match Keycloak exactly:
//   url      - Keycloak's PUBLIC address (the one in every token's `iss`)
//   realm    - the realm's name
//   clientId - the public client registered for this page
const keycloak = new Keycloak({
  url: 'http://localhost:8180',
  realm: 'lab',
  clientId: 'lab-web',
});

// Each endpoint, and who the API lets in. Used only to label the table;
// the API makes the actual decision.
const ENDPOINTS = [
  { path: '/api/me', who: 'any signed-in user' },
  { path: '/api/manager', who: 'manager or admin' },
  { path: '/api/admin', who: 'admin' },
];

const $ = (id) => document.getElementById(id);

// Declared up here because the top-level `await` below runs showSignedIn()
// before the module's later lines have executed.
let expiryTimer;

// --- 1. Start up --------------------------------------------------------------
// 'check-sso' = "if you already have a Keycloak session, sign me in quietly;
// if not, do nothing and let me show a Sign in button". It asks in a hidden
// iframe (silent-check-sso.html) so the page doesn't visibly redirect.
const authenticated = await keycloak.init({
  onLoad: 'check-sso',
  silentCheckSsoRedirectUri: `${location.origin}/silent-check-sso.html`,
  pkceMethod: 'S256', // the default already, written out so you can see it
  // The "login iframe" polls a Keycloak cookie to notice logouts in other
  // tabs. Browsers increasingly block that cookie, so we turn it off.
  checkLoginIframe: false,
});

$('status').hidden = true;
if (authenticated) {
  showSignedIn();
} else {
  $('signed-out').hidden = false;
}

// --- 2. Sign in / sign out ----------------------------------------------------
// login() sends the whole browser to Keycloak's login page. The password is
// typed THERE, never here. Keycloak then redirects back with a code.
$('sign-in').addEventListener('click', () => keycloak.login());

// logout() ends the Keycloak session (not just our local tokens), then
// Keycloak sends the browser back to this address. It must be allowed by the
// client's "post.logout.redirect.uris" setting.
$('sign-out').addEventListener('click', () =>
  keycloak.logout({ redirectUri: `${location.origin}/` }),
);

$('call-api').addEventListener('click', callApi);

// Access tokens are short-lived (5 minutes in this realm). When one expires,
// use the refresh token to get a new one, then re-render.
keycloak.onTokenExpired = async () => {
  try {
    await keycloak.updateToken(-1); // -1 = refresh now, whatever is left
    showSignedIn();
  } catch {
    // The refresh token is dead too (the Keycloak session ended).
    keycloak.login();
  }
};

// --- 3. Render the signed-in page --------------------------------------------
function showSignedIn() {
  // tokenParsed is the decoded access token payload. idTokenParsed is the ID
  // token. Display fields like the name come from the ID token's claims, but
  // Keycloak also copies them into the access token by default.
  const token = keycloak.tokenParsed;
  const roles = token.realm_access?.roles ?? [];

  $('signed-in').hidden = false;
  $('name').textContent = token.name || token.preferred_username;
  $('username').textContent = token.preferred_username;
  $('email').textContent = token.email ?? '(none)';
  $('roles').textContent = roles.join(', ');
  showExpiry();

  // Show a role's box only if the token carries that role. Cosmetic only!
  for (const box of document.querySelectorAll('[data-role]')) {
    box.hidden = !roles.includes(box.dataset.role);
  }

  // keycloak.token is the raw JWT string. Decode its header ourselves to show
  // that there is nothing secret about a JWT's contents.
  $('token-header').textContent = pretty(decodeJwtPart(keycloak.token.split('.')[0]));
  $('token-payload').textContent = pretty(token);

  callApi();
}

// --- 4. Call the API ---------------------------------------------------------
async function callApi() {
  // Make sure the token has at least 30 seconds left; refresh it if not.
  await keycloak.updateToken(30);

  const rows = await Promise.all(
    ENDPOINTS.map(async ({ path, who }) => {
      const response = await fetch(path, {
        headers: { Authorization: `Bearer ${keycloak.token}` },
      });
      const body = await response.json().catch(() => ({}));
      return { path, who, status: response.status, body };
    }),
  );

  const tbody = $('api-results');
  tbody.replaceChildren(
    ...rows.map(({ path, who, status, body }) => {
      const tr = document.createElement('tr');
      const ok = status === 200;
      tr.append(
        cell(path, 'mono'),
        cell(who),
        cell(`${status} ${ok ? 'allowed' : 'refused'}`, ok ? 'ok' : 'refused'),
        cell(body.message ?? body.detail ?? '', 'mono'),
      );
      return tr;
    }),
  );
}

// --- Small helpers ------------------------------------------------------------
function cell(text, className) {
  const td = document.createElement('td');
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

// JWT parts are base64url: '-' and '_' instead of '+' and '/', no padding.
function decodeJwtPart(part) {
  const base64 = part.replace(/-/g, '+').replace(/_/g, '/');
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

function pretty(object) {
  return JSON.stringify(object, null, 2);
}

function showExpiry() {
  clearInterval(expiryTimer);
  const tick = () => {
    const seconds = Math.round(keycloak.tokenParsed.exp - Date.now() / 1000);
    $('expires').textContent = seconds > 0 ? `${seconds} s` : 'expired, refreshing…';
  };
  tick();
  expiryTimer = setInterval(tick, 1000);
}
