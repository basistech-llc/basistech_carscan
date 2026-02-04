"""Tests for CarScan Lambda functions."""

import base64
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from boto3.dynamodb.conditions import Key

from app.carscan import s3_client, table
from app.main import lambda_handler

# When set, test_async_s3_handler uses mocks (no MinIO/DynamoDB). Cursor sets
# CURSOR_TRACE_ID in its terminal/test env; CI sets CI=true; or set CARSCAN_MOCK_S3_TEST=1.
def _use_s3_mocks():
    if os.environ.get("CARSCAN_MOCK_S3_TEST", "").lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("CI", "").lower() in ("1", "true", "yes"):
        return True
    # Cursor sets CURSOR_TRACE_ID (and possibly other CURSOR_*) in its terminal/test env
    if any(k.startswith("CURSOR_") for k in os.environ):
        return True
    return False


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
    key = s3_event["detail"]["object"]["key"]
    use_mocks = _use_s3_mocks()

    if not use_mocks and os.environ.get("AWS_REGION") != "local":
        pytest.skip(
            "test_async_s3_handler requires AWS_REGION=local and MinIO/DynamoDB Local, "
            "or set CARSCAN_MOCK_S3_TEST=1 / CI=1 to run with mocks"
        )

    if use_mocks:
        # Run with mocks so it passes in Cursor, CI, or sandbox (no real S3/DynamoDB)
        mock_table = MagicMock()
        saved_items = []

        def capture_put_item(**kwargs):
            item = kwargs.get("Item", {})
            saved_items.append(item)

        mock_table.put_item = MagicMock(side_effect=capture_put_item)
        mock_table.query.return_value = {
            "Items": [
                {
                    "user_email": "test@example.com",
                    "sk": f"job#{key}",
                    "plate": "ABC1234",
                    "state": "MA",
                    "result": "Stub",
                    "timestamp": 1234567890,
                    "image_key": key,
                }
            ]
        }

        with (
            patch("app.carscan.s3_client.head_object") as mock_head,
            patch("app.carscan._get_table", return_value=mock_table),
            patch("app.carscan.rekognition.detect_text") as mock_rek,
            patch("app.carscan.brivo.brivo_lookup", return_value="Stub"),
        ):
            mock_head.return_value = {
                "Metadata": {"user": "test@example.com", "state": "MA"}
            }
            mock_rek.return_value = {
                "TextDetections": [
                    {"DetectedText": "ABC1234", "Type": "LINE", "Confidence": 95}
                ],
            }
            lambda_handler(s3_event, {})

        assert mock_head.called
        assert mock_rek.called
        assert len(saved_items) == 1
        assert saved_items[0]["plate"] == "ABC1234"
        assert saved_items[0]["user_email"] == "test@example.com"
        return

    # Real MinIO + DynamoDB Local path
    try:
        s3_client.head_bucket(Bucket=bucket)
    except s3_client.exceptions.ClientError:
        s3_client.create_bucket(Bucket=bucket)

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=b"fake image data",
        Metadata={"user": "test@example.com", "state": "MA"},
    )

    with patch("app.carscan.rekognition.detect_text") as mock_rek:
        mock_rek.return_value = {
            "TextDetections": [
                {"DetectedText": "ABC1234", "Type": "LINE", "Confidence": 95}
            ],
        }
        lambda_handler(s3_event, {})

    response = table.query(
        KeyConditionExpression=Key("user_email").eq("test@example.com"),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    assert len(items) > 0, "Item should be saved to DynamoDB"
    saved_item = items[0]
    assert saved_item["plate"] == "ABC1234"
    assert saved_item["user_email"] == "test@example.com"
