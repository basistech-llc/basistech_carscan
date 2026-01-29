"""Pytest configuration and fixtures for CarScan tests."""

import os
import secrets as std_secrets
import socket
import threading
import time

import pytest

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
    from itsdangerous import URLSafeTimedSerializer

    app = Flask(__name__)
    serializer = URLSafeTimedSerializer(secret_key=hmac_secret, salt="oidc-state-v1")
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


@pytest.fixture
def api_event():
    """Generate a skeleton API Gateway HTTP API v2 event."""
    return {
        "version": "2.0",
        "routeKey": "GET /api/history",
        "rawPath": "/api/history",
        "rawQueryString": "",
        "headers": {"Cookie": "user_session=test@example.com"},
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
