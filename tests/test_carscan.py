import json
from unittest.mock import patch

import pytest

from app.main import lambda_handler

@pytest.fixture
def api_event():
    """Generates a skeleton API Gateway event."""
    return {
        "httpMethod": "GET",
        "path": "/api/history",
        "headers": {"Cookie": "user_session=test@example.com"},
        "requestContext": {"stage": "prod"},
        "queryStringParameters": {}
    }

@pytest.fixture
def s3_event():
    """Generates a skeleton S3 EventBridge event."""
    return {
        "source": "aws.s3",
        "detail": {
            "bucket": {"name": "test-bucket"},
            "object": {"key": "uploads/12345-test.jpg"}
        }
    }

def test_auth_middleware_denial(api_event):
    """Verifies that requests without a cookie are rejected with 401."""
    api_event["headers"] = {} # Remove auth
    response = lambda_handler(api_event, {})
    assert response["statusCode"] == 401
    assert "Unauthorized" in response["body"]

def test_upload_url_generation(api_event):
    """Verifies that the upload-url route returns a job_id and presigned data."""
    api_event["path"] = "/api/upload-url"
    api_event["queryStringParameters"] = {"state": "VA"}
    
    with patch("carscan.s3_client.generate_presigned_post") as mock_s3:
        mock_s3.return_value = {"url": "http://s3", "fields": {"key": "test"}}
        response = lambda_handler(api_event, {})
        
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "job_id" in body
        assert "presigned" in body

@patch("carscan.s3_client.head_object")
@patch("carscan.rekognition.detect_text")
@patch("carscan.table.put_item")
def test_async_s3_handler(mock_put, mock_rek, mock_head, s3_event):
    """Verifies the background S3 event processing logic."""
    # Mock S3 Metadata
    mock_head.return_value = {
        "Metadata": {"user": "test@example.com", "state": "MA"}
    }
    # Mock Rekognition finding a plate
    mock_rek.return_value = {
        "TextDetections": [{"DetectedText": "ABC1234", "Type": "LINE", "Confidence": 95}]
    }
    
    response = lambda_handler(s3_event, {})
    
    # Ensure the background handler processed the event
    assert mock_put.called
    saved_item = mock_put.call_args[1]["Item"]
    assert saved_item["plate"] == "ABC1234"
    assert saved_item["user_email"] == "test@example.com"
