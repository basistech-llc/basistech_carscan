"""Pytest configuration and fixtures for CarScan tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def set_test_env_vars(monkeypatch):
    """Set required environment variables for tests."""
    # Set AWS_REGION to local to use local endpoints
    monkeypatch.setenv("AWS_REGION", "local")
    # Use the actual table name from the DynamoDB JSON file
    monkeypatch.setenv("TABLE_NAME", "cala-garage-scans")
    monkeypatch.setenv("BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    # Set local AWS endpoints if not already set
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
