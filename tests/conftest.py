"""Pytest configuration and fixtures for CarScan tests."""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

# Fake IdP: lazy imports inside _make_fake_idp_server so tests that don't use
# fake_idp_server don't require jwt/cryptography/itsdangerous.


def _make_fake_idp_server(hmac_secret: str):
    """
    Create and return an HTTPServer with a handler that implements a minimal
    OIDC IdP (discovery, authorize redirect, jwks, token). Uses stdlib only
    for HTTP; no Flask. Server is bound to 127.0.0.1:0 (ephemeral port).
    """
    # pylint: disable=import-outside-toplevel
    import base64 as b64
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from itsdangerous import URLSafeTimedSerializer

    serializer = URLSafeTimedSerializer(secret_key=hmac_secret, salt="oidc-state-v1")
    code_store = {}
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    class FakeIdPHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _base_url(self):
            host, port = self.server.server_address
            return f"http://{host}:{port}"

        def log_message(self, format, *args):  # pylint: disable=arguments-differ
            pass

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def _send_error_body(self, message, status=400):
            self._send_json({"error": message}, status=status)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path == "/.well-known/openid-configuration":
                base = self._base_url()
                self._send_json({
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "jwks_uri": f"{base}/jwks",
                })
                return

            if path == "/authorize":
                state = (query.get("state") or [None])[0]
                redirect_uri = (query.get("redirect_uri") or [None])[0]
                if not state or not redirect_uri:
                    self._send_error_body("missing state or redirect_uri")
                    return
                try:
                    payload = serializer.loads(state)
                except Exception:  # pylint: disable=broad-except
                    self._send_error_body("invalid state")
                    return
                code = __import__("secrets").token_urlsafe(32)
                code_store[code] = {"nonce": payload["nonce"], "cv": payload["cv"], "state": state}
                loc = f"{redirect_uri}?code={code}&state={state}"
                self._send_redirect(loc)
                return

            if path == "/jwks":
                numbers = public_key.public_numbers()
                n_b64 = b64.urlsafe_b64encode(
                    numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
                ).rstrip(b"=").decode()
                e_b64 = b64.urlsafe_b64encode(numbers.e.to_bytes(3, "big")).rstrip(b"=").decode()
                self._send_json({"keys": [{"kty": "RSA", "kid": "fake-idp-1", "use": "sig", "alg": "RS256", "n": n_b64, "e": e_b64}]})
                return

            self.send_error(404)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != "/token":
                self.send_error(404)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
            form = parse_qs(body)
            code = (form.get("code") or [None])[0]
            state = (form.get("state") or [None])[0]
            code_verifier = (form.get("code_verifier") or [None])[0]
            if not code or not state or not code_verifier:
                self._send_error_body("missing code/state/code_verifier")
                return
            stored = code_store.get(code)
            if not stored:
                self._send_error_body("invalid code")
                return
            if stored["state"] != state:
                self._send_error_body("pkce verification failed")
                return
            if stored["cv"] != code_verifier:
                self._send_error_body("invalid code_verifier")
                return
            now = int(time.time())
            client_id = (form.get("client_id") or ["client-123"])[0]
            issuer = self._base_url()
            id_token_payload = {
                "iss": issuer,
                "sub": "alice-id",
                "aud": client_id,
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
            self._send_json({
                "access_token": "fake-access-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": id_token,
            })

    server = HTTPServer(("127.0.0.1", 0), FakeIdPHandler)
    return server


@pytest.fixture
def fake_idp_server():
    """
    Run a minimal in-process OIDC IdP server for tests (stdlib http.server, no Flask).

    Returns dict with "discovery" (URL to discovery document).
    Uses hmac_secret "super-secret-hmac" to decode state (must match test config).
    """
    server = _make_fake_idp_server(hmac_secret="super-secret-hmac")
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    def run():
        server.serve_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.3)
    try:
        yield {"discovery": f"{base_url}/.well-known/openid-configuration"}
    finally:
        server.shutdown()


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
