#!/usr/bin/env python3
"""Play the browser's part in a login, one printed step at a time.

    python3 scripts/login_flow_demo.py alice alice-dev    # one user, every step
    python3 scripts/login_flow_demo.py --all              # all three users, summary

This does by hand what keycloak-js does in web/app.js, so you can watch the
Authorization Code + PKCE flow without a library (docs/02-login-flow.md).

Note who types the password: this script fills in KEYCLOAK'S login form, as a
person would in a browser. It does not send the password to our app or use the
password grant (which the lab-web client has turned off).

Needs: Python 3.9+ and `pip install httpx`.
"""

import argparse
import base64
import hashlib
import html
import json
import re
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

KEYCLOAK = "http://localhost:8180/realms/lab/protocol/openid-connect"
PAGE = "http://localhost:3000"
CLIENT_ID = "lab-web"
REDIRECT_URI = f"{PAGE}/"
USERS = {"alice": "alice-dev", "bob": "bob-dev", "carol": "carol-dev"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def log_in(username: str, password: str, say=print) -> str:
    """Return an access token for `username`, printing each step via `say`."""
    browser = httpx.Client(follow_redirects=False, timeout=30)  # keeps cookies, like a browser

    # Step 1. The app invents a secret (the PKCE "code verifier") and keeps it.
    # It sends only a hash of it (the "code challenge") to Keycloak.
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)  # ties the reply to this request (anti-CSRF)
    say(f"1. PKCE verifier (kept secret by the app): {verifier}")
    say(f"   PKCE challenge = base64url(sha256(verifier)): {challenge}")

    # Step 2. Send the browser to Keycloak's authorization endpoint.
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{KEYCLOAK}/auth?{urlencode(params)}"
    say(f"2. Browser goes to: {auth_url}")
    login_page = browser.get(auth_url)
    if login_page.status_code != 200:
        sys.exit(f"   Keycloak refused the request ({login_page.status_code}): {login_page.text[:300]}")

    # Step 3. Keycloak shows ITS login page. The user types the password there.
    action = html.unescape(re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', login_page.text).group(1))
    say(f"3. Keycloak's login form posts to: {action[:90]}...")
    # Keycloak marks its login cookies `Secure`. Browsers send Secure cookies
    # to http://localhost (they treat localhost as a secure context); httpx
    # doesn't, so we pass them on by hand. Without them: "Cookie not found".
    cookies = "; ".join(f"{k}={v}" for k, v in login_page.cookies.items())
    answer = browser.post(action, data={"username": username, "password": password},
                          headers={"Cookie": cookies})
    if answer.status_code != 302:
        sys.exit(f"   Login failed for {username} (status {answer.status_code}). Wrong password?")

    # Step 4. Keycloak redirects back to the app with a one-time code.
    location = answer.headers["location"]
    returned = parse_qs(urlparse(location).fragment or urlparse(location).query)
    code = returned["code"][0]
    assert returned["state"][0] == state, "state mismatch: not our login"
    say(f"4. Keycloak redirects back to: {location[:90]}...")
    say(f"   one-time code: {code[:40]}...")

    # Step 5. The app swaps code + the ORIGINAL verifier for tokens. Keycloak
    # hashes the verifier and checks it matches the challenge from step 2, so
    # a stolen code is useless to anyone who doesn't have the verifier.
    tokens = httpx.post(f"{KEYCLOAK}/token", data={
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,  # public client: no secret
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }, timeout=30).json()
    if "access_token" not in tokens:
        sys.exit(f"   Token exchange failed: {tokens}")
    say(f"5. Token endpoint returned: {', '.join(k for k in tokens if k.endswith('token'))}")
    return tokens["access_token"]


def call_api(token: str) -> dict:
    """Call the three endpoints the way the page does (through nginx)."""
    results = {}
    for path in ("/api/me", "/api/manager", "/api/admin"):
        response = httpx.get(f"{PAGE}{path}", headers={"Authorization": f"Bearer {token}"})
        results[path] = (response.status_code, response.json())
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("username", nargs="?")
    parser.add_argument("password", nargs="?")
    parser.add_argument("--all", action="store_true", help="log in as every lab user and summarise")
    args = parser.parse_args()

    if args.all:
        print(f"{'user':<6} {'realm_access.roles':<44} {'aud':<22} /api/me  /api/manager  /api/admin")
        for user, password in USERS.items():
            token = log_in(user, password, say=lambda *_: None)
            claims = decode_jwt_payload(token)
            status = [str(code) for code, _ in call_api(token).values()]
            print(f"{user:<6} {str(claims['realm_access']['roles']):<44} {str(claims['aud']):<22} "
                  f"{status[0]:<8} {status[1]:<13} {status[2]}")
        return

    if not (args.username and args.password):
        parser.error("give a username and password, or --all")
    token = log_in(args.username, args.password)
    claims = decode_jwt_payload(token)
    print(f"6. Access token claims worth reading:")
    for claim in ("iss", "aud", "azp", "preferred_username", "realm_access", "exp"):
        print(f"   {claim}: {claims.get(claim)}")
    print("7. The app calls the API with 'Authorization: Bearer <access token>':")
    for path, (status, body) in call_api(token).items():
        print(f"   {path:<13} {status}  {body}")


if __name__ == "__main__":
    main()
