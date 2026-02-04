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
router = Router()                  # pylint: disable=not-callable
s3_client = boto3.client("s3")
dynamodb  = boto3.resource("dynamodb")
table     = dynamodb.Table(os.getenv("TABLE_NAME","cala-garage-scans"))

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
    if not user:
        # If context not set, this is an error - middleware should have caught this
        # But if it somehow got through, return empty list
        return []
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
    if not user:
        # If context not set, this is an error
        raise ValueError("user_email not found in context - authentication failed")
    body = router.current_event.json_body

    plate = body.get("plate", "").upper()

    # Generate a unique job_id for manual entries
    job_id = f"manual/{int(time.time())}"

    # Perform Brivo Lookup (currently stubbed)
    u = brivo.brivo_lookup(plate)

    item = {
        "user_email": user,
        "sk": f"job#{job_id}",
        "plate": plate,
        "result": u,
        "timestamp": int(time.time()),
        "image_key": "manual",
    }
    try:
        table.put_item(Item=item)
    except Exception as e:         # pylint: disable=broad-exception-caught
        logger.error("Exception: %s",e)

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
        if os.environ.get('AWS_REGION','')!='local':
            rekognition = boto3.client("rekognition")
            rek_resp = rekognition.detect_text( Image={"S3Object": {"Bucket": bucket, "Name": key}} )
            plate = next( ( t["DetectedText"] for t in rek_resp["TextDetections"] if t["Confidence"] > 90 ),
                          "NOT_FOUND")

            result_name = brivo.brivo_lookup(plate)
        else:
            result_name = None
            plate = "n/a"

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
