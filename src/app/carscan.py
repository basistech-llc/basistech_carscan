"""Core API routes and background processing for CarScan."""

import os
import time
from typing import Any, Dict

import boto3
from boto3.dynamodb.conditions import Key
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler.router import Router

from . import brivo

logger = Logger(child=True)
router = Router()  # pylint: disable=not-callable

# Initialize AWS clients
# Use local endpoints if AWS_REGION is "local"
_aws_region = os.environ.get("AWS_REGION", "")
_use_local = _aws_region == "local"

_s3_config = {}
_dynamodb_config = {}
if _use_local:
    _s3_endpoint = os.environ.get("AWS_ENDPOINT_URL_S3", "http://localhost:9100/")
    _dynamodb_endpoint = os.environ.get("AWS_ENDPOINT_URL_DYNAMODB", "http://localhost:8010/")
    if _s3_endpoint:
        _s3_config["endpoint_url"] = _s3_endpoint
    if _dynamodb_endpoint:
        _dynamodb_config["endpoint_url"] = _dynamodb_endpoint

s3_client = boto3.client("s3", **_s3_config)
rekognition = boto3.client("rekognition")
dynamodb = boto3.resource("dynamodb", **_dynamodb_config)


def _get_table():
    """Get the DynamoDB table, reading table name from environment dynamically."""
    table_name = os.environ.get("TABLE_NAME", "test-table")
    return dynamodb.Table(table_name)


# Lazy table accessor - reads table name from environment each time it's accessed
class _LazyTable:
    """Lazy table accessor that reads table name from environment each time."""

    def __getattr__(self, name):
        # Delegate all attribute access to the dynamically-created table
        return getattr(_get_table(), name)

    def __call__(self, *args, **kwargs):
        # Handle if table is called as a function (shouldn't happen, but be safe)
        return _get_table()(*args, **kwargs)


table = _LazyTable()


# --- API Routes ---

@router.get("/upload-url")
def get_upload_params() -> Dict[str, Any]:
    """Generate a presigned S3 POST URL with user identity in metadata."""
    # Get user_email from router context (set by middleware)
    user = router.context.get("user_email")
    if not user:
        # If context not set, this is an error - middleware should have set it
        raise ValueError("user_email not found in context - authentication failed")
    state = router.current_event.query_string_parameters.get("state", "MA")

    # Create a unique job ID based on timestamp and user
    job_id = f"uploads/{int(time.time())}-{user.split('@')[0]}.jpg"

    presigned = s3_client.generate_presigned_post(
        Bucket=os.environ["BUCKET_NAME"],
        Key=job_id,
        Fields={
            "x-amz-meta-user": user,
            "x-amz-meta-state": state,
            "Content-Type": "image/jpeg",
        },
        Conditions=[
            {"x-amz-meta-user": user},
            {"x-amz-meta-state": state},
            ["starts-with", "$Content-Type", "image/"],
        ],
        ExpiresIn=3600,
    )
    return {"presigned": presigned, "job_id": job_id}


@router.get("/status/<job_id>")
def get_scan_status(job_id: str) -> Dict[str, Any]:
    """Polled by the frontend to check if LPR is complete."""
    user = router.context.get("user_email")

    response = _get_table().get_item(
        Key={
            "user_email": user,
            "sk": f"job#{job_id}",
        }
    )

    item = response.get("Item")
    if not item:
        return {"status": "pending"}

    return {"status": "complete", "data": item}


@router.get("/history")
def get_user_history() -> list:
    """Return the last 50 scans for the logged-in user."""
    user = router.context.get("user_email")
    if not user:
        # If context not set, this is an error - middleware should have caught this
        # But if it somehow got through, return empty list
        return []
    resp = _get_table().query(
        KeyConditionExpression=Key("user_email").eq(user),
        ScanIndexForward=False,
        Limit=50,
    )
    return resp.get("Items", [])


@router.post("/manual")
def manual_entry() -> Dict[str, Any]:
    """Handle manual plate entry so it appears in history."""
    user = router.context.get("user_email")
    if not user:
        # If context not set, this is an error
        raise ValueError("user_email not found in context - authentication failed")
    body = router.current_event.json_body

    plate = body.get("plate", "").upper()
    state = body.get("state", "MA")

    # Generate a unique job_id for manual entries
    job_id = f"manual/{int(time.time())}"

    # Perform Brivo Lookup (currently stubbed)
    result_name = brivo.brivo_lookup(plate, state)

    item = {
        "user_email": user,
        "sk": f"job#{job_id}",
        "plate": plate,
        "state": state,
        "result": result_name,
        "timestamp": int(time.time()),
        "image_key": "manual",
    }
    _get_table().put_item(Item=item)

    return {"status": "complete", "data": item}


# --- Background Event Handling ---

def handle_s3_event(detail: Dict[str, Any]) -> None:
    """
    Triggered by S3 EventBridge.

    Performs LPR, Brivo lookup, and saves to DynamoDB.
    """
    bucket = detail["bucket"]["name"]
    key = detail["object"]["key"]

    try:
        # 1. Retrieve metadata stored during the presigned post
        head = s3_client.head_object(Bucket=bucket, Key=key)
        user = head["Metadata"].get("user")
        state = head["Metadata"].get("state", "MA")

        # 2. Perform LPR via Rekognition
        rek_resp = rekognition.detect_text(
            Image={"S3Object": {"Bucket": bucket, "Name": key}}
        )
        plate = next(
            (
                t["DetectedText"]
                for t in rek_resp["TextDetections"]
                if t["Confidence"] > 90
            ),
            "NOT_FOUND",
        )

        # 3. Lookup Brivo (currently stubbed)
        result_name = brivo.brivo_lookup(plate, state)

        # 4. Save record for frontend polling and history
        _get_table().put_item(
            Item={
                "user_email": user,
                "sk": f"job#{key}",
                "plate": plate,
                "state": state,
                "result": result_name,
                "timestamp": int(time.time()),
                "image_key": key,
            }
        )
        logger.info("Asynchronous scan complete for %s", key)

    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Background processing failed for %s: %s", key, exc)

