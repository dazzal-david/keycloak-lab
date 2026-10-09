"""keycloak-lab API: three endpoints, each guarded by a check on the access token.

Hiding a button in the browser is not security: anyone can open devtools and
call the API directly. So the API never trusts the page. On every request it
checks the token itself:

  1. signature  - was it signed by OUR Keycloak? (public keys from JWKS)
  2. issuer     - does `iss` name exactly our realm?
  3. expiry     - is `exp` still in the future?
  4. audience   - is the token addressed to this API (`aud` contains lab-api)?
  5. role       - does `realm_access.roles` contain a role this endpoint needs?

Failing 1-4 means "I don't know who you are" -> 401 Unauthorized.
Failing 5 means "I know who you are, and you may not do this" -> 403 Forbidden.
"""

import os

import jwt  # PyJWT
from fastapi import Depends, FastAPI, Header, HTTPException

# The issuer is the PUBLIC address, exactly as Keycloak writes it into `iss`.
ISSUER = os.environ["KEYCLOAK_ISSUER"]  # http://localhost:8180/realms/lab
# The keys are fetched over the Docker network. Inside this container,
# "localhost" is the API itself, so the public URL would not work here.
JWKS_URL = os.environ["KEYCLOAK_JWKS_URL"]  # http://keycloak:8080/realms/lab/...
AUDIENCE = os.environ["API_AUDIENCE"]  # lab-api

# PyJWKClient downloads Keycloak's public keys once and caches them. When a
# token arrives signed with a key id (`kid`) it has not seen, e.g. after a key
# rotation, it downloads the set again.
jwks = jwt.PyJWKClient(JWKS_URL, cache_keys=True)

app = FastAPI(title="keycloak-lab API")


def current_claims(authorization: str = Header(default="")) -> dict:
    """Return the verified token claims, or refuse with 401."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing 'Authorization: Bearer <token>' header")
    try:
        signing_key = jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            # Pin the algorithm. Never let the token's own header pick it.
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as error:
        # The exact reason ("Invalid issuer", "Signature has expired", ...) is
        # returned so learners can see it. A real API would only log it.
        raise HTTPException(401, f"Invalid token: {error}")


def realm_roles(claims: dict) -> list:
    # Realm roles live under realm_access.roles.
    # Client roles would be under resource_access.<client-id>.roles.
    return claims.get("realm_access", {}).get("roles", [])


def require_any_role(*allowed: str):
    """Build a dependency that passes only if the user has one of `allowed`."""

    def check(claims: dict = Depends(current_claims)) -> dict:
        if not set(allowed) & set(realm_roles(claims)):
            raise HTTPException(403, f"Needs one of these roles: {', '.join(allowed)}")
        return claims

    return check


@app.get("/api/me")
def me(claims: dict = Depends(current_claims)):
    """Any signed-in user."""
    return {
        "message": "You are signed in.",
        "username": claims.get("preferred_username"),
        "roles": realm_roles(claims),
    }


@app.get("/api/manager")
def manager_area(claims: dict = Depends(require_any_role("manager", "admin"))):
    return {"message": f"Manager data for {claims.get('preferred_username')}."}


@app.get("/api/admin")
def admin_area(claims: dict = Depends(require_any_role("admin"))):
    return {"message": f"Admin data for {claims.get('preferred_username')}."}
