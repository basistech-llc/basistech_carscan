"""
OIDC (OpenID Connect) helpers for CarScan.

Uses Google as the OIDC provider. Configuration is read from AWS Secrets Manager
using the secret ARN in GOOGLE_SECRET_ARN. The secret JSON must contain:
  - client_id, client_secret, redirect_uri
  - optionally oidc_discovery_endpoint (defaults to Google's well-known URL)

State parameter (CSRF/replay): We use itsdangerous.URLSafeTimedSerializer for
signed, time-limited state rather than JWT. Decision: itsdangerous is purpose-built
for this (dumps/loads with max_age), has clear exceptions (BadSignature,
SignatureExpired), and keeps the code simple. Using JWT for state would remove one
dependency but add more manual payload/options and less specific exceptions. We
keep itsdangerous unless we explicitly want to drop that dependency.
"""

import json
import os
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import boto3
import jwt
import requests
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from aws_lambda_powertools import Logger

LOGGER = Logger(child=True)

# Default Google OIDC discovery URL if not provided in secret
GOOGLE_OIDC_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"


def _normalize_google_secret(payload: dict) -> dict:
    """
    Normalize secret JSON to flat keys: client_id, client_secret, redirect_uri.

    Accepts either:
    - Google Cloud Console "web" client download: {"web": {"client_id", "client_secret", "redirect_uris": [...]}}
    - Flat format: {"client_id", "client_secret", "redirect_uri"}
    """
    if "web" in payload:
        web = payload["web"]
        uris = web.get("redirect_uris") or []
        redirect_uri = uris[0] if uris else ""
        return {
            "client_id": web["client_id"],
            "client_secret": web["client_secret"],
            "redirect_uri": redirect_uri,
            "oidc_discovery_endpoint": payload.get("oidc_discovery_endpoint"),
        }
    return {
        "client_id": payload["client_id"],
        "client_secret": payload["client_secret"],
        "redirect_uri": payload["redirect_uri"],
        "oidc_discovery_endpoint": payload.get("oidc_discovery_endpoint"),
    }


def get_oidc_config() -> dict:
    """
    Load OIDC config from AWS Secrets Manager using GOOGLE_SECRET_ARN.

    The secret JSON can be:
    - Google Cloud Console "web" client download (nested "web" with redirect_uris array), or
    - Flat: client_id, client_secret, redirect_uri; optional oidc_discovery_endpoint.

    Returns a merged dict suitable for load_openid_config + secret_key and hmac_secret.
    """
    secret_arn = os.environ.get("GOOGLE_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("GOOGLE_SECRET_ARN environment variable is not set")
    LOGGER.debug("Fetching OIDC secret", extra={"secret_arn": secret_arn})
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    raw = json.loads(response["SecretString"])
    payload = _normalize_google_secret(raw)
    if not payload.get("redirect_uri"):
        raise RuntimeError(
            "redirect_uri is missing or empty in the Google OIDC secret "
            "(for 'web' format, set redirect_uris in Google Cloud Console and re-download)"
        )
    discovery_url = payload.get("oidc_discovery_endpoint") or GOOGLE_OIDC_DISCOVERY
    config = load_openid_config(
        discovery_url,
        client_id=payload["client_id"],
        redirect_uri=payload["redirect_uri"],
    )
    config["secret_key"] = payload["client_secret"]
    config["hmac_secret"] = raw.get("hmac_secret", payload["client_secret"])
    return config



# Helper: stateless state serializer
def _state_serializer(secret_key: str) -> URLSafeTimedSerializer:
    # Change salt to rotate state format without changing your secret
    return URLSafeTimedSerializer(secret_key=secret_key, salt="oidc-state-v1")


def load_openid_config(discovery_url: str, *, client_id: str, redirect_uri: str) -> dict:
    """Fetch the contents of the discovery URL and create the openid config"""
    r = requests.get(discovery_url, timeout=10)
    r.raise_for_status()
    d = r.json()
    return {
        "issuer": d["issuer"],
        "authorization_endpoint": d["authorization_endpoint"],  # e.g. https://login.harvard.edu/oauth2/v1/authorize
        "token_endpoint": d["token_endpoint"],
        "jwks_uri": d["jwks_uri"],
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }


# -------- Function #1: Build Authorization URL (stateless) --------
# pylint: disable=too-many-locals
def build_oidc_authorization_url_stateless( *, openid_config: dict, scope=("openid", "profile", "email"), state_ttl_seconds=600 ):
    """
    Returns (authorization_url, issued_at_epoch) with state carrying nonce+PKCE code_verifier (signed).
    openid_config requires: authorization_endpoint, client_id, redirect_uri
    """
    auth_endpoint = openid_config["authorization_endpoint"]
    client_id     = openid_config["client_id"]
    redirect_uri  = openid_config["redirect_uri"]

    LOGGER.debug("client_id=%s", client_id)

    # PKCE
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")

    # CSRF + replay
    nonce = base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")
    issued_at = int(time.time())

    # Sign the state (contains only what's needed later)
    s = _state_serializer(openid_config['hmac_secret'])
    state_payload = {"nonce": nonce, "cv": code_verifier, "iat": issued_at, "ttl": state_ttl_seconds}
    state = s.dumps(state_payload)

    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scope),
        "state": state,
        "nonce": nonce,  # required so provider reflects it in ID token
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    parts = list(urlparse(auth_endpoint))
    existing = dict(parse_qsl(parts[4], keep_blank_values=True))
    existing.update(query)
    parts[4] = urlencode(existing)
    return urlunparse(parts), issued_at


# -------- Function #2: Handle Redirect (stateless) --------
# pylint: disable=too-many-locals
def handle_oidc_redirect_stateless(
    *,
    openid_config: dict,      # must include: token_endpoint, issuer, jwks_uri, client_id, redirect_uri
    callback_params: dict,    # API Gateway query params (GET)
    max_state_age_seconds=600 ):
    """
    Verifies signed state (age-limited), exchanges code with PKCE, verifies ID token, returns claims.
    """
    # 1) Validate redirect params
    if "error" in callback_params:
        raise RuntimeError(f"OIDC error: {callback_params.get('error')} - {callback_params.get('error_description')}")
    code  = callback_params.get("code")
    state = callback_params.get("state")
    if not code or not state:
        raise RuntimeError("Missing 'code' or 'state'.")

    # 2) Unpack & verify state (stateless)
    s = _state_serializer(openid_config['hmac_secret'])
    try:
        st = s.loads(state, max_age=max_state_age_seconds)
    except SignatureExpired as e:
        LOGGER.info("SignatureExpired: %s",e)
        raise
    except BadSignature as e:
        LOGGER.info("BadSignature: %s",e)
        raise

    code_verifier = st["cv"]
    expected_nonce = st["nonce"]

    token_endpoint = openid_config["token_endpoint"]
    issuer         = openid_config["issuer"]
    jwks_uri       = openid_config["jwks_uri"]
    client_id      = openid_config["client_id"]
    redirect_uri   = openid_config["redirect_uri"]
    client_secret  = openid_config['secret_key']

    # 3) Token exchange (confidential client with PKCE)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,          # some IdPs require it even with Basic auth
        "client_secret": client_secret,
        "code_verifier": code_verifier,  # binds the code to our request
    }
    resp = requests.post(token_endpoint, data=data, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Token endpoint error {resp.status_code}: {resp.text}")
    token_set = resp.json()

    id_token = token_set.get("id_token")
    if not id_token:
        raise RuntimeError("No id_token in token response.")
    access_token = token_set.get("access_token")

    # 4) Verify ID token (sig, iss, aud) and nonce
    jwk_client = jwt.PyJWKClient(jwks_uri)
    signing_key = jwk_client.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256", "ES256", "PS256", "RS384", "ES384", "PS384", "RS512", "ES512", "PS512"],
        audience=client_id,
        issuer=issuer,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )
    if claims.get("nonce") != expected_nonce:
        raise RuntimeError("Nonce mismatch.")

    # 5) Minimal profile/email extraction
    user = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "email_verified": claims.get("email_verified"),
        "name": claims.get("name"),
        "given_name": claims.get("given_name"),
        "family_name": claims.get("family_name"),
        "preferred_username": claims.get("preferred_username"),
        "updated_at": claims.get("updated_at"),
    }

    return {
        "id_token": id_token,
        "access_token": access_token,
        "expires_in": token_set.get("expires_in"),
        "scope": token_set.get("scope"),
        "token_type": token_set.get("token_type"),
        "claims": user,
    }
