"""Tests for CarScan Lambda functions."""

import json
from unittest.mock import patch

from boto3.dynamodb.conditions import Key

from app.carscan import s3_client, table
from app.main import lambda_handler


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
