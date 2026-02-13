"""Tests for CarScan Lambda functions."""

import base64
import json
import sys
import time
import uuid
from pathlib import Path
import os

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, BotoCoreError

import conftest as conftest_module
from app import carscan
from app.carscan import s3_client, table
from app.main import COOKIE_SALT as APP_COOKIE_SALT, lambda_handler, logger
from app.oidc import OIDC_STATE_SALT as APP_OIDC_SALT

PLATES_FAKE = Path(__file__).parent.parent / "data" / "plates_fake.json"


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

    try:
        s3_client.put_object( Bucket=bucket,
                              Key=key,
                              Body=b"fake image data",
                              Metadata={"user": email} )
    except (ClientError, BotoCoreError):
        # We use .get() so the logger doesn't crash if a variable is missing
        env_info = {
            "AWS_REGION": os.environ.get("AWS_REGION"),
            "AWS_PROFILE": os.environ.get("AWS_PROFILE"),
            "AWS_ENDPOINT_URL_S3": os.environ.get("AWS_ENDPOINT_URL_S3"),
        }
        logger.error("Region: %s",s3_client.meta.region_name)
        logger.error("Endpoint: %s",s3_client.meta.endpoint_url)
        logger.exception(
            f"AWS S3 PutObject failed. Environment context: {env_info}"
        )
        raise

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


def test_canonicalize_brivo_plates():
    plates = json.loads(PLATES_FAKE.read_text())
    cplates = carscan.canonicalize_brivo_plates(plates)
    assert len(plates) <= len(cplates)
    assert all( ( len(cplate['plate']) in [6,7] for cplate in cplates ) )


def test_all_plates_api_mocked(api_event):
    """GET /api/all-plates returns mocked data from data/plates_fake.json (no DynamoDB)."""
    from unittest.mock import patch # pylint: disable=import-outside-toplevel

    plates = json.loads(PLATES_FAKE.read_text())
    fake = carscan.canonicalize_brivo_plates(plates)
    with patch.object(carscan, "get_all_plates", return_value=fake):
        ev = _http_event("GET", "/api/all-plates")
        ev["headers"] = api_event["headers"]
        ev["routeKey"] = "GET /api/all-plates"
        ev["rawPath"] = "/api/all-plates"
        ev["requestContext"]["routeKey"] = "GET /api/all-plates"
        ev["requestContext"]["http"]["method"] = "GET"
        ev["requestContext"]["http"]["path"] = "/api/all-plates"
        response = lambda_handler(ev, {})
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert isinstance(body, list)
    assert len(body) > 0
    assert all("plate" in row and "name" in row for row in body)


def test_history_api_returns_user_scans(api_event):
    """GET /api/history returns scans for the authenticated user.

    Uses local DynamoDB. Inserts test items then verifies history returns them
    with correct shape (plate, result, timestamp, image_key, status).
    """
    user = "test@example.com"
    now = int(time.time())
    items_to_insert = [
        {
            "user_email": user,
            "sk": "job#uploads/1000-test.jpg",
            "plate": "ABC123",
            "result": {"firstName": "Jane", "lastName": "Doe"},
            "timestamp": now,
            "image_key": "uploads/1000-test.jpg",
        },
        {
            "user_email": user,
            "sk": "job#uploads/1001-test.jpg",
            "plate": None,
            "result": None,
            "timestamp": now - 100,
            "image_key": "uploads/1001-test.jpg",
        },
        {
            "user_email": user,
            "sk": "job#manual/2000",
            "plate": "NOT_FOUND",
            "result": None,
            "timestamp": now - 200,
            "image_key": "manual",
        },
    ]
    for item in items_to_insert:
        table.put_item(Item=item)

    ev = _http_event("GET", "/api/history")
    ev["headers"] = api_event["headers"]
    ev["routeKey"] = "GET /api/history"
    ev["rawPath"] = "/api/history"
    ev["requestContext"]["routeKey"] = "GET /api/history"
    ev["requestContext"]["http"]["method"] = "GET"
    ev["requestContext"]["http"]["path"] = "/api/history"
    response = lambda_handler(ev, {})

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert isinstance(body, list)
    assert len(body) >= 3

    by_sk = {it["sk"]: it for it in body}
    assert "job#uploads/1000-test.jpg" in by_sk
    assert "job#uploads/1001-test.jpg" in by_sk
    assert "job#manual/2000" in by_sk

    it1 = by_sk["job#uploads/1000-test.jpg"]
    assert it1["plate"] == "ABC123"
    assert it1["result"] == {"firstName": "Jane", "lastName": "Doe"}
    assert "timestamp" in it1
    assert it1["image_key"] == "uploads/1000-test.jpg"
    assert it1["status"] == "complete"

    it2 = by_sk["job#uploads/1001-test.jpg"]
    assert it2["plate"] is None
    assert it2["result"] is None
    assert it2["status"] == "complete"

    it3 = by_sk["job#manual/2000"]
    assert it3["plate"] == "NOT_FOUND"
    assert it3["result"] is None
    assert it3["image_key"] == "manual"
    assert it3["status"] == "manual"


def test_delete_scan_api(api_event):
    """DELETE /api/scan deletes DynamoDB entry and S3 object (if present)."""
    user = "test@example.com"
    now = int(time.time())
    sk = "job#manual/delete-test"
    table.put_item(Item={
        "user_email": user,
        "sk": sk,
        "plate": "DEL123",
        "result": None,
        "timestamp": now,
        "image_key": "manual",
    })

    ev = _http_event("DELETE", "/api/scan")
    ev["headers"] = api_event["headers"]
    ev["routeKey"] = "DELETE /api/scan"
    ev["rawPath"] = "/api/scan"
    ev["rawQueryString"] = f"sk={sk.replace('#', '%23')}"
    ev["queryStringParameters"] = {"sk": sk}
    ev["requestContext"]["routeKey"] = "DELETE /api/scan"
    ev["requestContext"]["http"]["method"] = "DELETE"
    ev["requestContext"]["http"]["path"] = "/api/scan"
    response = lambda_handler(ev, {})

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body.get("deleted") is True

    resp = table.get_item(Key={"user_email": user, "sk": sk})
    assert resp.get("Item") is None
