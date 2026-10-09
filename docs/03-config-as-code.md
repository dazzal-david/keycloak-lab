# 03 · Configuring Keycloak with code, not clicks

Clicking through the admin console works once. It is undocumented, can't be
reviewed, and can't be repeated on the next environment. Configuration you can
read, review and re-run lives in files.

This lab keeps **one desired-state file**, `keycloak/realm-export.json`, and
applies it two ways. That works because the realm-import format and the Admin
REST API use the same JSON shapes (Keycloak calls them *representations*): a
client in the import file is the same object you `POST` to `/admin/realms/lab/clients`.

## A. Realm import (`--import-realm`)

`docker-compose.yml` mounts the file into `/opt/keycloak/data/import/` and
starts Keycloak with `start-dev --import-realm`. On boot Keycloak reads it:

```
Realm 'lab' imported                          ← first start, empty database
Realm 'lab' already exists. Import skipped    ← every start after that
```

**The trap:** the import **skips a realm that already exists**. Edit the file,
restart Keycloak, and nothing changes. We hit this while building the lab:
we added `default-roles-lab` to the users in the file, restarted, and the log
said *Import skipped*. Only `configure.py --check` noticed the difference.

To make an edited import file take effect you must wipe the database:

```bash
docker compose down -v && docker compose up -d   # -v deletes the Postgres volume
```

**Use it for:** throwaway environments: laptops, CI, demos, a lab like this.
Declarative, zero tooling, fast.
**Don't use it for:** a running system with real users. Wiping the database to
change one redirect URI is not an option there.

## B. Admin REST API script (`scripts/configure.py`)

```bash
python3 scripts/configure.py --check   # what would change? (exit 1 if anything)
python3 scripts/configure.py --apply   # make it so
```

For each thing in the file (realm settings, roles, clients, mappers, users,
role assignments) the script **reads** the live value, **compares** it with the
file, and **writes only if they differ**. That makes it *idempotent*: running
`--apply` twice changes nothing the second time, and `--check` exiting 0
means "the server matches the file". Output after someone changed things by
hand in the console:

```
Client lab-web
  would: update client 'lab-web': directAccessGrantsEnabled: True -> False; redirectUris: ['*'] -> ['http://localhost:3000/*']
User bob
  would: roles of 'bob': add [], remove ['admin']

2 change(s) needed. Run --apply to make them.
```

Details worth noticing in the code:

- **Compare only what you declare.** Keycloak returns dozens of fields per
  object (ids, defaults). `differences()` looks only at keys in the file.
- **Read-modify-write on PUT.** Some PUT endpoints replace the whole object, so
  the script sends back what it read, with our fields changed (`merged()`).
- **Sub-resources have their own endpoints.** Protocol mappers, role mappings
  and passwords are not updated through the client/user object.
- **Passwords can't be compared.** Keycloak never returns them. They are set
  when a user is created, or for everyone with `--reset-passwords`.
- **It does not delete** what the file doesn't mention (a hand-made role
  survives). Deleting is where config tools do real damage, so the lab leaves
  it out. The one exception: each declared user ends up with exactly the
  declared lab roles, so moving bob from `manager` to `user` works.

### Who the script signs in as

**Not the master admin.** The master admin can change *every* realm, create
new admins and read every user. A leaked automation credential with that power
is a full compromise.

Instead the script uses **`lab-provisioner`**, a confidential client in the
`lab` realm with a **service account** (the client-credentials grant, no human,
no password) holding only these roles of the realm's built-in
`realm-management` client:

```
view-realm   manage-realm     realm settings, realm roles
view-clients manage-clients   clients, protocol mappers
view-users   manage-users     users, passwords, role assignments
```

It can't touch `master` or any other realm, its secret can be rotated without
touching a person's account, and every change is logged under its name.
(It's still powerful *inside* `lab`: `manage-users` can reset anyone's password
there. Least privilege narrows the blast radius; it doesn't remove it.)

### The bootstrap (step 0)

Something has to create the service account, and only the master admin can do
that the first time. So `--apply` does this, **only if the service account
can't sign in**:

1. Sign in as the master admin (password grant on master's `admin-cli` client,
   which is what `kcadm.sh` does).
2. Create the `lab` realm if it doesn't exist (empty; everything else comes
   later).
3. Create `lab-provisioner` with the secret from `.env`, and grant its service
   account the six roles above.

From then on the master admin is never used. Run `--apply` again and it prints
`Signed in as service account 'lab-provisioner' (master admin not needed)`.
In production you'd do step 0 once by hand or from a locked-down pipeline, then
store only the provisioner's secret in CI.

Because the script can create the realm itself, approach B works without A:
we tested deleting the whole `lab` realm and running `--apply`, which rebuilt
everything (12 changes) and then reported *In sync*.

**Use it for:** long-lived environments (staging, production), and anything
you want to apply repeatedly with drift detection (`--check` in CI).

## C. `kcadm.sh` (Keycloak's CLI): alternative, not built here

Ships inside the Keycloak image. It is a thin shell wrapper over the same Admin
REST API:

```bash
kc() { docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh "$@"; }
kc config credentials --server http://localhost:8080 --realm master --user admin --password admin-dev-only
kc get roles -r lab --fields name
kc create roles -r lab -s name=auditor
```

Good for one-off commands and quick shell scripts. It doesn't compare
anything, so idempotency is your job (`create` fails if the thing exists).

## D. Terraform `keycloak` provider: alternative, not built here

The community provider `keycloak/keycloak` (formerly `mrparkers/keycloak`)
describes realms, clients, roles and mappers as Terraform resources.
`terraform plan` is our `--check`, `terraform apply` is our `--apply`, and
unlike our script it also **deletes** what you remove from the code, because
it keeps a *state file* of what it created.

Good when your team already runs Terraform for the rest of its infrastructure.
Costs: state file to protect (it can contain secrets), and importing existing
hand-made config into state is tedious.

## Which to use when

| Situation | Use |
|---|---|
| Laptop, CI, demo, "start fresh every time" | **A. Import** |
| Existing server, small team, want to see the code | **B. REST script** |
| One-off fix or investigation | **C. kcadm.sh** |
| Team already manages infrastructure with Terraform | **D. Terraform** |

## Changing config safely later

1. Edit `keycloak/realm-export.json`.
2. Run `--check` and read the diff. It's your review: is that what you meant?
3. Commit the file change; get it reviewed like code.
4. Run `--apply` against staging, test, then production.
5. Run `--check` again: it must say *In sync*.
6. Never fix things in the admin console "just this once". If you must,
   `--check` will show the drift; put it in the file or let `--apply` undo it.

Before an upgrade or a risky change, take a database backup. The realm export
in the admin console (*Realm settings → Action → Partial export*) is handy for
comparison, but it leaves out secrets and users.

---

## Production notes (traps we've hit)

### `start-dev` vs `start`

`start-dev` is for laptops: plain HTTP allowed, hostname optional, caches and
theme caching off. Production uses `start`, which refuses to run without:

- **A hostname:** `KC_HOSTNAME=https://auth.example.com` (or with a path, see
  below). It fixes the issuer in every token.
- **HTTPS:** either Keycloak's own certificate (`KC_HTTPS_CERTIFICATE_FILE`…)
  or TLS ended at a load balancer with `KC_HTTP_ENABLED=true` behind it.
- **Proxy headers:** behind a load balancer, `KC_PROXY_HEADERS=xforwarded` (or
  `forwarded`) so Keycloak knows the original scheme, host and client IP. Only
  enable it when a proxy you control sets those headers, or clients can spoof them.
- **A relative path, if shared:** to serve Keycloak at
  `https://app.example.com/kc`, set `KC_HTTP_RELATIVE_PATH=/kc` and put the
  path in the hostname too (`KC_HOSTNAME=https://app.example.com/kc`). The
  issuer becomes `https://app.example.com/kc/realms/<realm>`, and the API must
  expect exactly that.
- **An optimized build:** run `kc.sh build` in your image, then
  `start --optimized` for fast startup.
- **Health/metrics** on the management port (9000), not exposed publicly.

### The bootstrap admin only works once

`KC_BOOTSTRAP_ADMIN_USERNAME` / `PASSWORD` are read only when the database is
**empty**, to create the first master admin. After that they're ignored: change
them in `.env` and nothing happens, because that user already exists in the
database. To change that password later, change it in Keycloak (console or
`kcadm.sh set-password`). If you've lost it, run
`kc.sh bootstrap-admin user` against the same database. In production, sign in
once, create a real named admin (with two-factor), then delete or disable the
bootstrap user.

### Keep the admin console and `master` off the internet

The `master` realm and the admin console are the keys to every realm: one
leaked or brute-forced admin password and an attacker can mint themselves any
role in any app, read every user and turn off every protection. Your users
never need them; they only need `/realms/<your-realm>/…` (login pages, tokens,
JWKS) and `/resources/…` (CSS, JS). So at the load balancer, allow only those
paths from the internet and block `/admin/` and `/realms/master/`, or serve the
admin side on a separate hostname (`KC_HOSTNAME_ADMIN`) reachable only from a
VPN or office network. This lab binds every port to `127.0.0.1` for the same
reason: nothing here is reachable from your network.
