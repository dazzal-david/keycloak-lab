#!/usr/bin/env python3
"""Configure Keycloak through its Admin REST API, idempotently.

    python3 scripts/configure.py --check   # report what would change, change nothing
    python3 scripts/configure.py --apply   # make Keycloak match the file

The desired state is keycloak/realm-export.json, the SAME file Keycloak imports
on first boot. The import file and the Admin REST API use the same JSON shapes
("representations"), so one file can drive both methods:

  * import (--import-realm) creates the realm once and then ignores the file,
  * this script compares the file with the live server and fixes differences,
    every time you run it.

Idempotent means running --apply twice changes nothing the second time: each
step first READS the current value and only WRITES when it differs.

Who signs in: a dedicated service-account client, `lab-provisioner`, that lives
in the lab realm and holds only a few `realm-management` roles. It cannot touch
the master realm or any other realm. The all-powerful master admin is used only
for "step 0": creating that client the first time (or fixing its secret).

Scope: the script creates and updates what the file declares. It does not
delete things the file doesn't mention (e.g. a role added by hand), except that
each declared user ends up with exactly the declared lab roles.

Passwords: Keycloak never returns a password, so there is nothing to compare.
A password is set when a user is created, or for everyone with --reset-passwords.

Needs: Python 3.9+, `pip install httpx`, and the values in .env.
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
KEYCLOAK_URL = "http://localhost:8180"
REALM_FILE = ROOT / "keycloak" / "realm-export.json"

PROVISIONER_CLIENT_ID = "lab-provisioner"
# Least privilege: enough to manage realm settings, roles, clients and users
# in ONE realm. (The master admin can do anything to every realm.)
PROVISIONER_ROLES = [
    "view-realm", "manage-realm",      # realm settings and realm roles
    "view-clients", "manage-clients",  # clients and their protocol mappers
    "view-users", "manage-users",      # users, passwords, role assignments
]

# Parts of a representation that have their own endpoints, so they are
# handled separately rather than in a field-by-field comparison.
REALM_PARTS = {"roles", "clients", "users"}
CLIENT_PARTS = {"protocolMappers"}
USER_PARTS = {"realmRoles", "credentials"}


# --- Reading .env -------------------------------------------------------------
def load_env() -> dict:
    """Tiny .env reader, so the script needs no extra package for it."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit("No .env file. Run: cp .env.example .env")
    values = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


# --- Comparing desired with current -------------------------------------------
def differences(desired, current, path=""):
    """List the places where `current` does not match `desired`.

    Only keys that appear in `desired` are compared: Keycloak returns many more
    fields (defaults, ids, timestamps) that we don't manage.
    Lists are compared ignoring order.
    """
    found = []
    if isinstance(desired, dict):
        current = current if isinstance(current, dict) else {}
        for key, value in desired.items():
            found += differences(value, current.get(key), f"{path}.{key}" if path else key)
    elif isinstance(desired, list):
        if sorted(map(json.dumps, desired)) != sorted(map(json.dumps, current or [])):
            found.append(f"{path}: {current} -> {desired}")
    elif desired != current:
        found.append(f"{path}: {current!r} -> {desired!r}")
    return found


def merged(current: dict, desired: dict) -> dict:
    """`current` with `desired` laid on top (nested dicts merged, not replaced).

    Used for PUT: send back everything Keycloak gave us with our values changed,
    so the fields we don't manage stay as they are.
    """
    result = dict(current)
    for key, value in desired.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = value
    return result


def without(representation: dict, keys: set) -> dict:
    return {k: v for k, v in representation.items() if k not in keys}


# --- Talking to Keycloak --------------------------------------------------------
class AdminApi:
    """A thin wrapper over /admin/realms/<realm>/...

    get() returns None for 404 ("doesn't exist yet"); every other error stops
    the script and prints Keycloak's own message.
    """

    def __init__(self, token: str, realm: str = ""):
        base = f"{KEYCLOAK_URL}/admin/realms" + (f"/{realm}" if realm else "")
        self.http = httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )

    def get(self, path: str, **params):
        response = self.http.get(path, params=params)
        if response.status_code == 404:
            return None
        return self._checked(response).json()

    def find(self, path: str, **params):
        """GET a search endpoint and return the single match, or None."""
        results = self.get(path, **params)
        return results[0] if results else None

    def post(self, path: str, body):
        return self._checked(self.http.post(path, json=body))

    def put(self, path: str, body):
        return self._checked(self.http.put(path, json=body))

    def delete(self, path: str, body=None):
        return self._checked(self.http.request("DELETE", path, json=body))

    @staticmethod
    def _checked(response: httpx.Response) -> httpx.Response:
        if response.is_error:
            sys.exit(
                f"Keycloak answered {response.status_code} to "
                f"{response.request.method} {response.request.url}:\n{response.text}"
            )
        return response


def request_token(realm: str, **form):
    """POST to a realm's token endpoint. Returns the access token, or None."""
    response = httpx.post(
        f"{KEYCLOAK_URL}/realms/{realm}/protocol/openid-connect/token", data=form, timeout=30
    )
    return response.json()["access_token"] if response.status_code == 200 else None


# --- Recording and applying changes --------------------------------------------
class Changes:
    """Prints every change. In --apply mode it also performs it."""

    def __init__(self, apply: bool):
        self.apply = apply
        self.count = 0

    def make(self, description: str, action) -> None:
        self.count += 1
        print(f"  {'apply' if self.apply else 'would'}: {description}")
        if self.apply:
            action()


# --- Step 0: bootstrap with the master admin ------------------------------------
def bootstrap(realm: str, env: dict, changes: Changes) -> None:
    """Create the realm if missing, and the provisioner client with its roles."""
    print("Step 0: bootstrap (master admin)")
    # The password grant on master's built-in `admin-cli` client is how
    # kcadm.sh and scripts sign in as the master admin. This is the one place
    # the lab uses the password grant, and only to create the service account.
    token = request_token(
        "master",
        grant_type="password",
        client_id="admin-cli",
        username=env["KC_BOOTSTRAP_ADMIN_USERNAME"],
        password=env["KC_BOOTSTRAP_ADMIN_PASSWORD"],
    )
    if not token:
        sys.exit("Master admin sign-in failed. Are KC_BOOTSTRAP_ADMIN_* in .env the "
                 "values Keycloak was FIRST started with?")
    master = AdminApi(token)

    if master.get(f"/{realm}") is None:
        changes.make(f"create realm '{realm}' (empty; filled in below)",
                     lambda: master.post("", {"realm": realm, "enabled": True}))

    desired = {
        "clientId": PROVISIONER_CLIENT_ID,
        "name": "Lab provisioner (scripts/configure.py)",
        "description": "Service account for config-as-code. No browser login.",
        "enabled": True,
        "publicClient": False,  # confidential: it has a secret
        "secret": env["LAB_PROVISIONER_SECRET"],
        "serviceAccountsEnabled": True,  # can get tokens as itself
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
    }
    lab = AdminApi(token, realm)
    current = lab.find("/clients", clientId=PROVISIONER_CLIENT_ID)
    if current is None:
        changes.make(f"create client '{PROVISIONER_CLIENT_ID}'", lambda: lab.post("/clients", desired))
    else:
        # The secret is never shown in the client representation, so just
        # write the whole thing; bootstrap only runs when sign-in failed anyway.
        changes.make(f"update client '{PROVISIONER_CLIENT_ID}' (secret from .env)",
                     lambda: lab.put(f"/clients/{current['id']}", merged(current, desired)))

    def grant_roles():
        client = lab.find("/clients", clientId=PROVISIONER_CLIENT_ID)
        account = lab.get(f"/clients/{client['id']}/service-account-user")
        # realm-management is a built-in client in every realm. Its client
        # roles are the admin permissions for that realm.
        rm = lab.find("/clients", clientId="realm-management")
        roles = [lab.get(f"/clients/{rm['id']}/roles/{name}") for name in PROVISIONER_ROLES]
        lab.post(f"/users/{account['id']}/role-mappings/clients/{rm['id']}", roles)

    changes.make(f"give the service account realm-management roles: {', '.join(PROVISIONER_ROLES)}",
                 grant_roles)


# --- Step 1+: reconcile the realm as the provisioner -----------------------------
def reconcile_realm_settings(api: AdminApi, realm: dict, changes: Changes) -> None:
    print("Realm settings")
    desired = without(realm, REALM_PARTS)
    diff = differences(desired, api.get(""))
    if diff:
        # A realm PUT only changes the fields you send.
        changes.make("update " + "; ".join(diff), lambda: api.put("", desired))


def reconcile_roles(api: AdminApi, roles: list, changes: Changes) -> None:
    print("Realm roles")
    for role in roles:
        current = api.get(f"/roles/{role['name']}")
        if current is None:
            changes.make(f"create role '{role['name']}'", lambda r=role: api.post("/roles", r))
            continue
        diff = differences(role, current)
        if diff:
            changes.make(f"update role '{role['name']}': " + "; ".join(diff),
                         lambda r=role, c=current: api.put(f"/roles/{r['name']}", merged(c, r)))


def reconcile_client(api: AdminApi, client: dict, changes: Changes) -> None:
    print(f"Client {client['clientId']}")
    current = api.find("/clients", clientId=client["clientId"])
    if current is None:
        # Creating a client accepts its protocol mappers in the same request.
        changes.make(f"create client '{client['clientId']}'", lambda: api.post("/clients", client))
        return

    desired = without(client, CLIENT_PARTS)
    diff = differences(desired, current)
    if diff:
        changes.make(f"update client '{client['clientId']}': " + "; ".join(diff),
                     lambda: api.put(f"/clients/{current['id']}", merged(current, desired)))

    # Protocol mappers have their own endpoint; match them by name.
    mappers_path = f"/clients/{current['id']}/protocol-mappers/models"
    existing = {m["name"]: m for m in api.get(mappers_path)}
    for mapper in client.get("protocolMappers", []):
        found = existing.get(mapper["name"])
        if found is None:
            changes.make(f"add mapper '{mapper['name']}'",
                         lambda m=mapper: api.post(mappers_path, m))
            continue
        diff = differences(mapper, found)
        if diff:
            changes.make(f"update mapper '{mapper['name']}': " + "; ".join(diff),
                         lambda m=mapper, f=found: api.put(f"{mappers_path}/{f['id']}", merged(f, m)))


def reconcile_user(api: AdminApi, user: dict, lab_roles: set, reset_password: bool,
                   changes: Changes) -> None:
    print(f"User {user['username']}")
    desired = without(user, USER_PARTS)
    password = next((c for c in user.get("credentials", []) if c["type"] == "password"), None)
    current = api.find("/users", username=user["username"], exact="true")

    if current is None:
        def create():
            api.post("/users", desired)
            created = api.find("/users", username=user["username"], exact="true")
            if password:
                api.put(f"/users/{created['id']}/reset-password", password)
            set_roles(api, created["id"], set(user.get("realmRoles", [])), lab_roles)

        changes.make(f"create user '{user['username']}' with roles {user.get('realmRoles', [])}", create)
        return

    diff = differences(desired, current)
    if diff:
        changes.make(f"update user '{user['username']}': " + "; ".join(diff),
                     lambda: api.put(f"/users/{current['id']}", merged(current, desired)))

    if reset_password and password:
        changes.make(f"reset password of '{user['username']}'",
                     lambda: api.put(f"/users/{current['id']}/reset-password", password))

    # Roles: the user should have exactly the declared lab roles. Other roles
    # (like default-roles-lab) are not ours to manage, so they are left alone.
    has = {r["name"] for r in api.get(f"/users/{current['id']}/role-mappings/realm")}
    wanted = set(user.get("realmRoles", []))
    add, remove = wanted - has, (has & lab_roles) - wanted
    if add or remove:
        changes.make(f"roles of '{user['username']}': add {sorted(add)}, remove {sorted(remove)}",
                     lambda: set_roles(api, current["id"], wanted, lab_roles))


def set_roles(api: AdminApi, user_id: str, wanted: set, lab_roles: set) -> None:
    path = f"/users/{user_id}/role-mappings/realm"
    has = {r["name"] for r in api.get(path)}
    # The role-mapping endpoints want full role objects (id + name), not names.
    add = [api.get(f"/roles/{name}") for name in wanted - has]
    remove = [api.get(f"/roles/{name}") for name in (has & lab_roles) - wanted]
    if add:
        api.post(path, add)
    if remove:
        api.delete(path, remove)


# --- Main ------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report differences, change nothing")
    mode.add_argument("--apply", action="store_true", help="make Keycloak match the file")
    parser.add_argument("--reset-passwords", action="store_true",
                        help="also set every user's password from the file")
    args = parser.parse_args()

    env = load_env()
    realm = json.loads(REALM_FILE.read_text())
    name = realm["realm"]
    changes = Changes(apply=args.apply)

    token = request_token(name, grant_type="client_credentials",
                          client_id=PROVISIONER_CLIENT_ID,
                          client_secret=env["LAB_PROVISIONER_SECRET"])
    if token is None:
        if not args.apply:
            print(f"The '{PROVISIONER_CLIENT_ID}' service account can't sign in yet.")
            print("Run --apply once: it bootstraps that account with the master admin.")
            sys.exit(1)
        bootstrap(name, env, changes)
        token = request_token(name, grant_type="client_credentials",
                              client_id=PROVISIONER_CLIENT_ID,
                              client_secret=env["LAB_PROVISIONER_SECRET"])
    else:
        print(f"Signed in as service account '{PROVISIONER_CLIENT_ID}' (master admin not needed)")

    api = AdminApi(token, name)
    reconcile_realm_settings(api, realm, changes)
    reconcile_roles(api, realm["roles"]["realm"], changes)
    # lab-api first: lab-web's audience mapper refers to it.
    for client in sorted(realm["clients"], key=lambda c: c["clientId"] != "lab-api"):
        reconcile_client(api, client, changes)
    lab_roles = {r["name"] for r in realm["roles"]["realm"]}
    for user in realm["users"]:
        reconcile_user(api, user, lab_roles, args.reset_passwords, changes)

    print()
    if changes.count == 0:
        print("In sync: nothing to change.")
    elif args.apply:
        print(f"Applied {changes.count} change(s). Run --check to confirm it is now in sync.")
    else:
        print(f"{changes.count} change(s) needed. Run --apply to make them.")
        sys.exit(1)  # non-zero so CI can fail on drift


if __name__ == "__main__":
    main()
