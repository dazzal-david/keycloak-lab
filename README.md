# keycloak-lab

A small teaching project for learning **Keycloak**, **OpenID Connect** and
**role-based access control (RBAC)**: a web page you sign in to through Keycloak,
three users with different roles, and an API that checks those roles itself.
Everything, including Keycloak's configuration, is done in code.

It's for learning, not a product: few files, short ones, comments that explain
*why*.

## What's in it

| Piece | What it does |
|---|---|
| **Keycloak 26** + Postgres | The identity provider. Realm `lab`, roles `admin` / `manager` / `user`. |
| **web/** (nginx, plain HTML + JS) | Sign in, see your name, roles and decoded token, see which API calls succeed. Uses the official `keycloak-js` adapter from a CDN. |
| **api/** (Python, FastAPI) | `/api/me` (anyone signed in), `/api/manager` (manager or admin), `/api/admin` (admin). Verifies signature, issuer, expiry, audience and role. |
| **keycloak/realm-export.json** | The desired configuration: realm, roles, clients, users. |
| **scripts/configure.py** | Applies that file through the Admin REST API. Idempotent; `--check` / `--apply`. |
| **scripts/login_flow_demo.py** | Does the browser's login by hand and prints each step. |

## Run it

Needs Docker, and Python 3.9+ with `httpx` for the scripts (`pip install httpx`).

```bash
cp .env.example .env
docker compose up -d                     # first start takes ~30 s; Keycloak imports the realm
python3 scripts/configure.py --apply     # creates the provisioner service account, checks everything
python3 scripts/configure.py --check     # should print "In sync: nothing to change."
```

Open **http://localhost:3000** (use `localhost`, not `127.0.0.1`; see
[docs/02](docs/02-login-flow.md#traps-on-this-path)) in Chrome or Firefox.

| | URL |
|---|---|
| Page | http://localhost:3000 |
| API | http://localhost:8000/api/me (needs a token; the page calls it via `/api/`) |
| Keycloak admin console | http://localhost:8180/admin (`admin` / `admin-dev-only`) |

All ports are bound to `127.0.0.1`: reachable from this machine only.

## The three users

Dev-only passwords. Don't reuse them anywhere.

| User | Password | Role | Sees section | /api/me | /api/manager | /api/admin |
|---|---|---|---|---|---|---|
| alice | `alice-dev` | admin | admin | 200 | 200 | 200 |
| bob | `bob-dev` | manager | manager | 200 | 200 | 403 |
| carol | `carol-dev` | user | user | 200 | 403 | 403 |

Check all three from the terminal without a browser:

```bash
python3 scripts/login_flow_demo.py --all
```

## Reset everything

```bash
docker compose down -v      # -v also deletes Keycloak's database volume
docker compose up -d
python3 scripts/configure.py --apply
```

You need `-v` whenever you want Keycloak to re-import an edited
`realm-export.json` or re-read the bootstrap admin from `.env`. Both are only
read when the database is empty.

## Read the docs in this order

1. [**Concepts**](docs/01-concepts.md): realm, client, user, roles, scopes, the
   three tokens, issuer, JWKS, audience, 401 vs 403.
2. [**The login flow**](docs/02-login-flow.md): what happens from "Sign in" to
   the API's answer, step by step, plus the redirect-URI, Web-Origins and
   password-grant traps.
3. [**Config as code**](docs/03-config-as-code.md): realm import vs the REST
   script vs `kcadm.sh` vs Terraform; changing config safely; production
   notes (`start` vs `start-dev`, bootstrap admin, keeping `master` private).
4. [**Exercises**](docs/04-exercises.md): eight hands-on changes, each with a
   prediction to make first.

## Where each production trap is explained

| Trap | Where |
|---|---|
| Issuer must match exactly; two URLs for one Keycloak | [01 · Issuer](docs/01-concepts.md#issuer-iss), exercise 4 |
| Public client + PKCE, no password grant | [01 · Client](docs/01-concepts.md#client), [02 · Why never collect passwords](docs/02-login-flow.md#why-the-app-must-never-collect-passwords) |
| Redirect URIs and Web Origins | [02 · Traps](docs/02-login-flow.md#traps-on-this-path) |
| Roles in the token, default roles | [01 · Roles](docs/01-concepts.md#roles-realm-roles-vs-client-roles) |
| Audience (`aud`) | [01 · Audience](docs/01-concepts.md#audience-aud) |
| `start-dev` vs `start` | [03 · Production notes](docs/03-config-as-code.md#start-dev-vs-start) |
| Bootstrap admin only works once | [03 · Production notes](docs/03-config-as-code.md#the-bootstrap-admin-only-works-once) |
| Admin console and `master` off the internet | [03 · Production notes](docs/03-config-as-code.md#keep-the-admin-console-and-master-off-the-internet) |

## Layout

```
docker-compose.yml        four services: postgres, keycloak, api, web
.env.example              bootstrap admin, db password, provisioner secret (.env is gitignored)
keycloak/realm-export.json  desired state: imported on first boot AND read by configure.py
scripts/configure.py      Admin REST API reconciler (--check / --apply)
scripts/login_flow_demo.py  the login flow by hand; --all tests the three users
web/                      index.html, app.js, style.css, silent-check-sso.html
nginx/default.conf        serves web/, forwards /api/ to the API
api/                      main.py, requirements.txt, Dockerfile
docs/                     01–04, read in order
```
