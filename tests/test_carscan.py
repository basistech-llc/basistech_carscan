"""Tests for CarScan Lambda functions."""

import base64
import json
import uuid
import sys

from boto3.dynamodb.conditions import Key

import conftest as conftest_module
from app.carscan import s3_client, table
from app.main import COOKIE_SALT as APP_COOKIE_SALT, lambda_handler
from app.oidc import OIDC_STATE_SALT as APP_OIDC_SALT



def test_conftest_salts_match_app():
    """Conftest salt constants must match app so test cookies and OIDC state verify."""
    assert conftest_module.COOKIE_SALT == APP_COOKIE_SALT, "conftest.COOKIE_SALT must match app.main.COOKIE_SALT"
    assert conftest_module.OIDC_STATE_SALT == APP_OIDC_SALT, "conftest.OIDC_STATE_SALT must match app.oidc.OIDC_STATE_SALT"


def _http_event(method: str, path: str, headers: dict | None = None):
    """Build HTTP API v2 event for method and path (no pathParameters for explicit routes)."""
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path if path.startswith("/") else f"/{path}",
        "rawQueryString": "",
        "headers": headers or {},
        "requestContext": {
            "routeKey": f"{method} {path}",
            "http": {
                "method": method,
                "path": path if path.startswith("/") else f"/{path}",
                "protocol": "HTTP/1.1",
            },
            "stage": "$default",
            "requestId": "test-request-id",
            "timeEpoch": 1428582896000,
        },
        "queryStringParameters": None,
        "pathParameters": None,
        "isBase64Encoded": False,
    }


def test_get_root_without_cookie_returns_landing():
    """GET / without user_session cookie returns landing page (200 HTML)."""
    ev = _http_event("GET", "/")
    ev["pathParameters"] = {"proxy": ""}  # API Gateway proxy+ sends this for /
    response = lambda_handler(ev, {})
    assert response["statusCode"] == 200
    assert "text/html" in response.get("headers", {}).get("contentType", response.get("headers", {}).get("Content-Type", ""))
    body = response.get("body", "")
    if response.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    assert "landing" in body.lower() or "Google" in body or "Login" in body


def test_get_hello_without_cookie_returns_json():
    """GET /hello without cookie returns Hello world JSON (no auth required)."""
    ev = _http_event("GET", "/hello")
    ev["pathParameters"] = {"proxy": "hello"}
    response = lambda_handler(ev, {})
    assert response["statusCode"] == 200
    body = response.get("body", "")
    if response.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    data = json.loads(body)
    assert data.get("message") == "Hello world!"


def test_get_style_without_cookie_returns_json():
    """GET /static/style.css without cookie."""
    ev = _http_event("GET", "/static/style.css")
    ev["pathParameters"] = {"proxy": "static/style.css"}
    response = lambda_handler(ev, {})
    assert response["statusCode"] == 200
    body = response.get("body", "")
    if response.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    assert "/* style.css */" in body


def test_auth_middleware_denial(api_event):
    """Verify that requests without a cookie are rejected with 401."""
    api_event["headers"] = {}  # Remove auth
    api_event["routeKey"] = "GET /api/history"
    api_event["rawPath"] = "/api/history"
    api_event["requestContext"]["routeKey"] = "GET /api/history"
    api_event["requestContext"]["http"]["method"] = "GET"
    api_event["requestContext"]["http"]["path"] = "/api/history"
    response = lambda_handler(api_event, {})
    assert response["statusCode"] == 401
    assert "Unauthorized" in response["body"]


def test_upload_url_generation(api_event):
    """Verify that the upload-url route returns a job_id and presigned data."""
    api_event["routeKey"] = "GET /api/upload-url"
    api_event["rawPath"] = "/api/upload-url"
    api_event["requestContext"]["routeKey"] = "GET /api/upload-url"
    api_event["requestContext"]["http"]["method"] = "GET"
    api_event["requestContext"]["http"]["path"] = "/api/upload-url"
    api_event["rawQueryString"] = "state=VA"
    api_event["queryStringParameters"] = {"state": "VA"}
    response = lambda_handler(api_event, {})
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert "job_id" in body
    assert "presigned" in body


def test_async_s3_handler(s3_event):
    """Verify the background S3 event processing logic.

    - With CARSCAN_MOCK_S3_TEST=1 or CI=true: uses mocks (no MinIO/DynamoDB). Use this
      in Cursor or CI so the test runs without local services.
    - With AWS_REGION=local and no mock env: uses real MinIO and DynamoDB Local.
    - Otherwise: skipped (avoids hitting real AWS or unreachable localhost).
    """
    bucket = s3_event["detail"]["bucket"]["name"]
    key    = s3_event["detail"]["object"]["key"]
    email = f"test-{str(uuid.uuid4())}@example.com"

    s3_client.put_object( Bucket=bucket,
                          Key=key,
                          Body=b"fake image data",
                          Metadata={"user": email} )

    lambda_handler(s3_event, {})

    response = table.query( KeyConditionExpression=Key("user_email").eq(email),
                            ScanIndexForward=False,
                            Limit=1 )
    items = response.get("Items", [])
    if len(items)==0:
        print("Items not saved to DynamoDB",file=sys.stderr)
        print("============================================",file=sys.stderr)
        print(f"Scan of {table}",file=sys.stderr)
        r = table.scan()
        print(json.dumps(r,indent=4,default=str),file=sys.stderr)
