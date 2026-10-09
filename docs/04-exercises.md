# 04 · Exercises

Do them in order. Each one changes one thing and asks you to **predict what will
happen before you look**. All changes go through files and
`scripts/configure.py`, never the admin console. You can *look* in the console
(http://localhost:8180/admin, `admin` / `admin-dev-only`) to see the effect.

Reset everything at any time: `docker compose down -v && docker compose up -d && python3 scripts/configure.py --apply`.

---

## 1. Hiding is not security

Sign in as **carol** (role `user`).

1. Open devtools → Elements, find `<section class="role" data-role="admin" hidden>`
   and delete the `hidden` attribute. The admin box appears. Did you just become
   an admin?
2. Copy carol's token from devtools → Network (any `/api/me` request →
   `Authorization` header, without the word `Bearer`) and call the API
   yourself: `curl -i -H "Authorization: Bearer <token>" http://localhost:8000/api/admin`.
3. Now edit the token: decode the payload (any base64 tool), add `"admin"` to
   `realm_access.roles`, re-encode, send it again.

**Expect:** 403 with the real token; with the edited one, 401 *Signature
verification failed*. The API trusts the signature, not the page.

## 2. Add a fourth role

Add a role `auditor` and give it to carol.

1. In `keycloak/realm-export.json`, add `{ "name": "auditor", "description": "…" }`
   to `roles.realm`, and add `"auditor"` to carol's `realmRoles`.
2. Restart Keycloak: `docker compose restart keycloak`. Check carol's token.
   Predict first: will it have `auditor`?
3. Run `python3 scripts/configure.py --check`, then `--apply`. Sign carol out
   and in again.
4. Add a `data-role="auditor"` section to `web/index.html` and a
   `GET /api/audit` endpoint to `api/main.py` with `require_any_role("auditor", "admin")`
   (then `docker compose up -d --build api`). Add it to `ENDPOINTS` in `app.js`.

**Expect:** step 2 changes nothing (log: *Import skipped*). Step 3 shows
`create role 'auditor'` and a role change for carol. Old tokens keep the old
roles until they're refreshed or you sign in again, because roles are copied
into the token at the moment it's issued.

## 3. Watch a token expire and refresh

1. Set `"accessTokenLifespan": 60` in the realm file. `--check`, `--apply`.
2. Sign in. Watch **Token expires in** count down from 60.
3. Open devtools → Network, filter `token`. Wait for zero.

**Expect:** at zero, `onTokenExpired` in `app.js` fires, a `POST …/token` with
`grant_type=refresh_token` appears, the countdown restarts, and the page
re-calls the API. The `iat` and `jti` in the decoded token change; `sid` (the
Keycloak session) doesn't.

Bonus: set `"ssoSessionIdleTimeout": 120` too, leave the tab alone for 2+
minutes and see what happens when the refresh token dies.

Put both values back afterwards.

## 4. Break the issuer, then the audience, on purpose

1. In `docker-compose.yml`, change the API's `KEYCLOAK_ISSUER` to
   `http://localhost:8180/realms/lab/` (one trailing slash). Run
   `docker compose up -d api` and click **Call the API again**.
2. Put it back. Now open the page at **http://127.0.0.1:3000** instead of
   localhost and click Sign in.
3. Put it back. Remove the `protocolMappers` block from `lab-web` in the realm
   file. `--check` will say nothing. Why? (Hint: read `reconcile_client`: it
   adds and updates mappers but never deletes.) Delete the mapper in the admin
   console instead (Clients → lab-web → Client scopes → lab-web-dedicated), then
   sign in again.

**Expect:** (1) every endpoint 401 *Invalid issuer*. (2) Keycloak's error page
*Invalid parameter: redirect_uri*: `127.0.0.1:3000` isn't in the client's
redirect URIs. (3) `aud` is just `account`, every endpoint 401 *Audience
doesn't match*. Then `--apply` puts the mapper back. Restore the file.

## 5. Add a client role

Client roles belong to one client. Give `lab-api` a role `reports-read` and
give it to bob.

1. Use `kcadm.sh` (see [03](03-config-as-code.md#c-kcadmsh-keycloaks-cli-alternative-not-built-here)):
   ```bash
   # a shell function, not KC="...": zsh won't split a variable into words
   kc() { docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh "$@"; }
   kc config credentials --server http://localhost:8080 --realm master --user admin --password admin-dev-only
   API_ID=$(kc get clients -r lab -q clientId=lab-api --fields id --format csv --noquotes)
   kc create clients/$API_ID/roles -r lab -s name=reports-read
   kc add-roles -r lab --uusername bob --cclientid lab-api --rolename reports-read
   ```
2. Sign in as bob and find the role in the decoded token.
3. Run `configure.py --check`. Does it notice?

**Expect:** a new entry `resource_access.lab-api.roles: ["reports-read"]` (tested).
`--check` says *In sync*, because the script only manages what it was written to
manage. **Stretch:** teach `configure.py` client roles (import format:
`"roles": { "client": { "lab-api": [ … ] } }`; endpoints
`/clients/{id}/roles` and `/users/{id}/role-mappings/clients/{id}`), and make
the API's `/api/manager` also accept `reports-read`.

## 6. Sign in with an email address

1. Sign out, then sign in with **alice@lab.local** / `alice-dev`. It works:
   `loginWithEmailAllowed` is `true` in the realm file.
2. Set `"loginWithEmailAllowed": false`, `--apply`, try again.
3. Set it back, and add `"registrationEmailAsUsername": true`. `--apply`, then
   look at the login form and at alice's user in the console. Predict: does
   alice's username change?

**Expect:** (2) *Invalid username or password*. (3) The form's label changes
to "Email". Whether existing usernames change is the point of the exercise:
look before you assume, and compare `preferred_username` in a fresh token.
This is a decision you want to make *before* you have real users.

## 7. Add a required action

Required actions are things Keycloak makes a user do at their next login.

1. Add `"requiredActions": ["UPDATE_PASSWORD"]` to carol in the realm file.
   `--apply`. Sign in as carol.
2. After she's changed her password, run `--check`.

**Expect:** (1) Keycloak asks for a new password before returning to the
page; our app did nothing. (2) `--check` reports drift: the action was
completed and cleared, but the file still asks for it. Some fields are
*one-time commands*, not *desired state*. Take it out of the file and use
`--reset-passwords` to put carol's password back. Try `CONFIGURE_TOTP` too:
two-factor login, without touching our code.

## 8. Revoke a user: how long do they keep access?

1. Sign in as bob in one window.
2. In the realm file set bob's `"enabled": false`. `--apply`.
3. In bob's window, click **Call the API again**. Then wait for the token to
   expire (or use exercise 3's 60-second setting).

**Expect:** the API **keeps saying 200** until the access token expires,
because it checks tokens offline (signature + expiry) and never asks Keycloak
whether bob is still allowed. When the page tries to refresh, Keycloak refuses
and bob is sent to the login page. This is the trade-off of stateless
tokens: short access-token lifetimes are your revocation delay. If you need
instant revocation, the API can call Keycloak's *token introspection*
endpoint on each request, at the cost of a network call per request.
Set `enabled` back to `true`.
