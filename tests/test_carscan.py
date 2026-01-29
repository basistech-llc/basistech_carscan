"""Tests for CarScan Lambda functions."""

import base64
import json
from unittest.mock import patch

from boto3.dynamodb.conditions import Key

from app.carscan import s3_client, table
from app.main import lambda_handler


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
    """Verify the background S3 event processing logic."""
    # This test uses real S3 (MinIO) and DynamoDB Local
    # Rekognition will still need to be mocked since it's AWS-only
    bucket = s3_event["detail"]["bucket"]["name"]
    key = s3_event["detail"]["object"]["key"]
    
    # Ensure bucket exists
    try:
        s3_client.head_bucket(Bucket=bucket)
    except s3_client.exceptions.ClientError:
        # Bucket doesn't exist, create it
        s3_client.create_bucket(Bucket=bucket)
    
    # Create the S3 object in MinIO with metadata
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=b"fake image data",
        Metadata={
            "user": "test@example.com",
            "state": "MA",
        },
    )
    
    with patch("app.carscan.rekognition.detect_text") as mock_rek:
        # Mock Rekognition finding a plate (AWS service, not local)
        mock_rek.return_value = {
            "TextDetections": [
                {"DetectedText": "ABC1234", "Type": "LINE", "Confidence": 95}
            ],
        }
        
        lambda_handler(s3_event, {})
        
        # Verify the item was saved to DynamoDB by querying it
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
