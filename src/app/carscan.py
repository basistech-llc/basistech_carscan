"""Core API routes and background processing for CarScan."""

import os
import time
from typing import Any, Dict

import boto3
from boto3.dynamodb.conditions import Key
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import Router

from . import brivo

logger = Logger(child=True)
router = Router()  # pylint: disable=not-callable

# Initialize AWS clients
s3_client = boto3.client("s3")
rekognition = boto3.client("rekognition")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])


# --- API Routes ---

@router.get("/upload-url")
def get_upload_params() -> Dict[str, Any]:
    """Generate a presigned S3 POST URL with user identity in metadata."""
    user = router.context.get("user_email")
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

    response = table.get_item(
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
    resp = table.query(
        KeyConditionExpression=Key("user_email").eq(user),
        ScanIndexForward=False,
        Limit=50,
    )
    return resp.get("Items", [])


@router.post("/manual")
def manual_entry() -> Dict[str, Any]:
    """Handle manual plate entry so it appears in history."""
    user = router.context.get("user_email")
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
    table.put_item(Item=item)

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
        table.put_item(
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

