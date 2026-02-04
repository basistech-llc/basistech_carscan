"""Pytest configuration and fixtures for CarScan tests.

test_async_s3_handler uses mocks (no MinIO/DynamoDB) when any of:
  - Cursor is active (Cursor sets CURSOR_TRACE_ID or other CURSOR_* in env)
  - CI=true (e.g. GitHub Actions)
  - CARSCAN_MOCK_S3_TEST=1
Otherwise it requires AWS_REGION=local and MinIO/DynamoDB Local, or skips.
"""

import io
import json
import os
import secrets as std_secrets
import socket
import threading
import time
import urllib.request
from unittest.mock import patch
from urllib.parse import parse_qsl, urlparse

from itsdangerous import URLSafeTimedSerializer
import pytest

# Must match app.main.COOKIE_SALT and app.oidc.OIDC_STATE_SALT (test_conftest_salts_match_app enforces).
COOKIE_SALT = "user_session"
OIDC_STATE_SALT = "oidc-state-v1"

# -----------------------------------------------------------------------------
# Mock OIDC IdP (no socket bind): patches requests.get/post so OIDC tests run
# without starting a real server. Use fixture mock_oidc_idp in OIDC tests.
# -----------------------------------------------------------------------------

# Fake base URL for discovery/token/jwks (no server actually running)
MOCK_OIDC_BASE = "https://fake-idp.test/oidc"


class _MockResponse:
    """Minimal response object for requests mock (status_code, headers, .json(), .text, .raise_for_status())."""

    def __init__(self, status_code: int, body, headers=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        if isinstance(body, dict):
            self._body = json.dumps(body)
            self.headers.setdefault("Content-Type", "application/json")
        else:
            self._body = str(body)

    @property
    def text(self):
        return self._body

    def json(self):
        return json.loads(self._body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


@pytest.fixture
def mock_oidc_idp(monkeypatch):
    """
    Mock OIDC IdP by patching requests.get and requests.post. No server or socket bind.

    Yields dict with "discovery" URL. Use this fixture in OIDC tests so they run
    in environments that cannot bind ports (e.g. CI/sandbox).
    """
    # pylint: disable=import-outside-toplevel
    import base64 as b64
    from cryptography.hazmat.primitives.asymmetric import rsa

    try:
        import jwt
    except ImportError:
        pytest.skip("PyJWT/cryptography required for OIDC tests")

    issuer = MOCK_OIDC_BASE.rstrip("/")
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    authorization_endpoint = f"{issuer}/authorize"
    token_endpoint = f"{issuer}/token"
    jwks_uri = f"{issuer}/jwks"

    hmac_secret = "super-secret-hmac"
    serializer = URLSafeTimedSerializer(secret_key=hmac_secret, salt=OIDC_STATE_SALT)
    code_store = {}

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    numbers = public_key.public_numbers()
    n_b64 = b64.urlsafe_b64encode(
        numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    ).rstrip(b"=").decode()
    e_b64 = b64.urlsafe_b64encode(numbers.e.to_bytes(3, "big")).rstrip(b"=").decode()
    jwks_keys = [
        {"kty": "RSA", "kid": "fake-idp-1", "use": "sig", "alg": "RS256", "n": n_b64, "e": e_b64}
    ]

    def mock_get(url, timeout=None, **kwargs):  # pylint: disable=unused-argument
        if url == discovery_url:
            return _MockResponse(
                200,
                {
                    "issuer": issuer,
                    "authorization_endpoint": authorization_endpoint,
                    "token_endpoint": token_endpoint,
                    "jwks_uri": jwks_uri,
                },
            )
        if url == jwks_uri:
            return _MockResponse(200, {"keys": jwks_keys})
        # Authorize: URL contains state and redirect_uri; return 302 with Location
        parsed = urlparse(url)
        qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
        state = qs.get("state")
        redirect_uri = qs.get("redirect_uri")
        if not state or not redirect_uri:
            return _MockResponse(400, {"error": "missing state or redirect_uri"})
        try:
            payload = serializer.loads(state)
        except Exception:  # pylint: disable=broad-except
            return _MockResponse(400, {"error": "invalid state"})
        code = std_secrets.token_urlsafe(32)
        code_store[code] = {"nonce": payload["nonce"], "cv": payload["cv"], "state": state}
        loc = f"{redirect_uri}?code={code}&state={state}"
        return _MockResponse(302, "", headers={"Location": loc})

    def mock_post(url, timeout=None, data=None, **kwargs):  # pylint: disable=unused-argument
        if url != token_endpoint:
            raise RuntimeError(f"Unexpected POST: {url}")
        data = data or {}
        code = data.get("code")
        code_verifier = data.get("code_verifier")
        if not code or not code_verifier:
            return _MockResponse(400, {"error": "missing code/state/code_verifier"})
        if code not in code_store:
            return _MockResponse(400, {"error": "invalid code"})
        stored = code_store.pop(code)
        if stored["cv"] != code_verifier:
            return _MockResponse(400, {"error": "invalid code_verifier"})
        now = int(time.time())
        id_token_payload = {
            "iss": issuer,
            "sub": "alice-id",
            "aud": data.get("client_id", "client-123"),
            "exp": now + 3600,
            "iat": now,
            "nonce": stored["nonce"],
            "email": "alice@example.edu",
            "email_verified": True,
            "name": "Alice Example",
        }
        id_token = jwt.encode(
            id_token_payload,
            private_key,
            algorithm="RS256",
            headers={"kid": "fake-idp-1"},
        )
        if hasattr(id_token, "decode"):
            id_token = id_token.decode("utf-8")
        return _MockResponse(
            200,
            {
                "access_token": "fake-access-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": id_token,
            },
        )

    monkeypatch.setattr("requests.get", mock_get)
    monkeypatch.setattr("requests.post", mock_post)
    # app.oidc uses requests; ensure the module sees the patched requests
    monkeypatch.setattr("app.oidc.requests.get", mock_get)
    monkeypatch.setattr("app.oidc.requests.post", mock_post)

    # PyJWKClient uses urllib.request.urlopen for JWKS; patch so no real HTTP
    original_urlopen = urllib.request.urlopen

    def mock_urlopen(req, timeout=None, context=None):
        url = getattr(req, "full_url", None) or (
            getattr(req, "get_full_url", lambda: None)() if hasattr(req, "get_full_url") else None
        )
        if not url and isinstance(req, str):
            url = req
        if url == jwks_uri:
            body = json.dumps({"keys": jwks_keys}).encode()

            class _MockJWKSResponse(io.BytesIO):
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass

            return _MockJWKSResponse(body)
        return original_urlopen(req, timeout=timeout, context=context)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    yield {"discovery": discovery_url}

    code_store.clear()


# -----------------------------------------------------------------------------
# Real Fake IdP server (requires socket bind; use mock_oidc_idp when binding is not allowed)
# -----------------------------------------------------------------------------
# Fake IdP: lazy imports inside _make_fake_idp_app so tests that don't use
# fake_idp_server don't require flask/jwt/cryptography/itsdangerous.


def _make_fake_idp_app(hmac_secret: str, base_url: str):
    """
    Create Flask app for fake OIDC IdP (E11-style). base_url is e.g. http://127.0.0.1:PORT.
    Flask is dev-only; it sends complete HTTP responses (e.g. redirect with body) so clients don't hang.
    """
    # pylint: disable=import-outside-toplevel
    import base64 as b64
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from flask import Flask, redirect, request

    app = Flask(__name__)
    serializer = URLSafeTimedSerializer(secret_key=hmac_secret, salt=OIDC_STATE_SALT)
    code_store = {}
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    issuer = base_url.rstrip("/")

    @app.route("/.well-known/openid-configuration")
    def discovery():
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{base_url}/authorize",
            "token_endpoint": f"{base_url}/token",
            "jwks_uri": f"{base_url}/jwks",
        }

    @app.route("/authorize")
    def authorize():
        state = request.args.get("state")
        redirect_uri = request.args.get("redirect_uri")
        if not state or not redirect_uri:
            return {"error": "missing state or redirect_uri"}, 400
        try:
            payload = serializer.loads(state)
        except Exception:  # pylint: disable=broad-except
            return {"error": "invalid state"}, 400
        code = std_secrets.token_urlsafe(32)
        code_store[code] = {"nonce": payload["nonce"], "cv": payload["cv"], "state": state}
        loc = f"{redirect_uri}?code={code}&state={state}"
        return redirect(loc, code=302)

    @app.route("/jwks")
    def jwks():
        numbers = public_key.public_numbers()
        n_b64 = b64.urlsafe_b64encode(
            numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
        ).rstrip(b"=").decode()
        e_b64 = b64.urlsafe_b64encode(numbers.e.to_bytes(3, "big")).rstrip(b"=").decode()
        return {"keys": [{"kty": "RSA", "kid": "fake-idp-1", "use": "sig", "alg": "RS256", "n": n_b64, "e": e_b64}]}

    @app.route("/token", methods=["POST"])
    def token():
        code = request.form.get("code")
        state_from_form = request.form.get("state")  # optional; app may not send it
        code_verifier = request.form.get("code_verifier")
        if not code or not code_verifier:
            return {"error": "missing code/state/code_verifier"}, 400
        stored = code_store.get(code)
        if not stored:
            return {"error": "invalid code"}, 400
        if state_from_form is not None and stored["state"] != state_from_form:
            return {"error": "pkce verification failed"}, 400
        if stored["cv"] != code_verifier:
            return {"error": "invalid code_verifier"}, 400
        nonce = stored["nonce"]
        now = int(time.time())
        id_token_payload = {
            "iss": issuer,
            "sub": "alice-id",
            "aud": request.form.get("client_id", "client-123"),
            "exp": now + 3600,
            "iat": now,
            "nonce": nonce,
            "email": "alice@example.edu",
            "email_verified": True,
            "name": "Alice Example",
        }
        id_token = jwt.encode(
            id_token_payload,
            private_key,
            algorithm="RS256",
            headers={"kid": "fake-idp-1"},
        )
        if hasattr(id_token, "decode"):
            id_token = id_token.decode("utf-8")
        return {
            "access_token": "fake-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "id_token": id_token,
        }

    return app


@pytest.fixture
def fake_idp_server():
    """
    Run a minimal in-process OIDC IdP server for tests (Flask, dev dependency).

    Returns dict with "discovery" (URL to discovery document).
    Uses hmac_secret "super-secret-hmac" to decode state (must match test config).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base_url = f"http://127.0.0.1:{port}"
    app = _make_fake_idp_app(hmac_secret="super-secret-hmac", base_url=base_url)

    def run():
        app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.5)
    yield {"discovery": f"{base_url}/.well-known/openid-configuration"}


@pytest.fixture(autouse=True)
def set_test_env_vars(monkeypatch):
    """Set required environment variables for tests."""
    monkeypatch.setenv("AWS_REGION", "local")
    monkeypatch.setenv("TABLE_NAME", "cala-garage-scans")
    monkeypatch.setenv("BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    if "AWS_ENDPOINT_URL_DYNAMODB" not in os.environ:
        monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", "http://localhost:8010/")
    if "AWS_ENDPOINT_URL_S3" not in os.environ:
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://localhost:9100/")
    if "AWS_ACCESS_KEY_ID" not in os.environ:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    if "AWS_SECRET_ACCESS_KEY" not in os.environ:
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "minioadmin")


# Cookie signer secret used in tests. main.py uses oidc.get_oidc_config_hash() in production;
# we patch that when loading main so the app's cookie signer uses this value and test cookies verify.
_TEST_COOKIE_SECRET = "hardcoded-secret-changeme"

# Load app.main with get_oidc_config_hash patched so _cookie_signer is created with _TEST_COOKIE_SECRET.
# This must run before any test imports app.main (conftest loads first).
with patch("app.oidc.get_oidc_config_hash", return_value=_TEST_COOKIE_SECRET):
    import app.main as _app_main  # noqa: E402, F401


def _signed_user_session(email: str) -> str:
    """Return a signed user_session cookie value (same secret/salt as main)."""
    signer = URLSafeTimedSerializer(secret_key=_TEST_COOKIE_SECRET, salt=COOKIE_SALT)
    return signer.dumps(email)


@pytest.fixture
def api_event():
    """Generate a skeleton API Gateway HTTP API v2 event with valid signed session cookie."""
    signed = _signed_user_session("test@example.com")
    return {
        "version": "2.0",
        "routeKey": "GET /api/history",
        "rawPath": "/api/history",
        "rawQueryString": "",
        "headers": {"Cookie": f"user_session={signed}"},
        "requestContext": {
            "routeKey": "GET /api/history",
            "http": {
                "method": "GET",
                "path": "/api/history",
                "protocol": "HTTP/1.1",
            },
            "stage": "$default",
            "requestId": "test-request-id",
            "time": "09/Apr/2015:12:34:56 +0000",
            "timeEpoch": 1428582896000,
        },
        "queryStringParameters": None,
        "isBase64Encoded": False,
    }


@pytest.fixture
def s3_event():
    """Generate a skeleton S3 EventBridge event."""
    return {
        "source": "aws.s3",
        "detail": {
            "bucket": {"name": "test-bucket"},
            "object": {"key": "uploads/12345-test.jpg"},
        },
    }
