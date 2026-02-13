"""Core API routes and background processing for CarScan.

DynamoDB Table:
primary key: 'user_email'
sort key: 'sk'

user_email - person who is logged in (e.g. simsong@basistech.com)
sk - job#{job_id}
 - contents - - status on an image that has been uploaded

user_email - 'config'
sk - '#'

"""

import re
import os
import time
from typing import Any, Dict
import json
import argparse
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler.router import Router
from aws_lambda_powertools.event_handler import Response

from . import brivo
from . import fuzzy_match

logger = Logger(child=True)
router = Router()                  # pylint: disable=not-callable
s3_client = boto3.client("s3")

dynamodb  = boto3.resource("dynamodb", region_name=os.getenv('AWS_REGION'))
table     = dynamodb.Table(os.getenv("TABLE_NAME","cala-garage-scans"))

################################################################
# Datbase Routines

def save_all_plates(verbose=False,store_file:Path|None = None):
    """save all the brivo plates
    """
    plates = brivo.dump_all_plates(verbose=verbose)
    if store_file is not None:
        store_file.write_text(json.dumps(plates,indent=4,default=str))
    table.put_item(Item={'user_email':'plates',
                         'sk':'plates',
                         'plates':plates,
                         'timestamp':int(time.time())})


RE_PARENS = re.compile(r"\(.*\)")
def canonicalize_brivo_plates(obj_array):
    """Given the database of brivo plates, transform into an array of objects with 'plate' and 'name'"""
    out = []
    for obj in obj_array:
        work = RE_PARENS.sub("", obj.get('plate', '') or '')
        work = work.replace(" ", "")
        if not work:
            continue
        if "," in work:
            plates = work.split(",")
        elif "&" in work:
            plates = work.split("&")
        elif "-" in work:
            # the "-" might be in a single plate or it might separate two plates.
            # If everything separated is larger than 4 characters, it is separating two plates
            candidates = work.split("-")
            if all( (len(candidate) > 4 for candidate in candidates ) ):
                plates = candidates
            else:
                plates = [work.replace("-","")]
        else:
            plates = [work]
        for plate in plates:
            p = plate.strip()
            if not p:
                continue
            out.append({'plate': p, 'name': obj['firstName'] + ' ' + obj['lastName']})
    out.sort(key=lambda a:a['plate'])
    return out

def get_all_plates():
    item = table.get_item(Key={'user_email': 'plates', 'sk': 'plates'}).get('Item')
    if not item or 'plates' not in item:
        return []
    return canonicalize_brivo_plates(item['plates'])
# --- API Routes ---

@router.get("/upload-url")
def get_upload_params() -> Dict[str, Any]:
    """Generate a presigned S3 POST URL with user identity in metadata."""
    # Get user_email from router context (set by middleware)
    user = router.context.get("user_email")
    if not user:
        # If context not set, this is an error - middleware should have set it
        raise ValueError("user_email not found in context - authentication failed")
    # Create a unique job ID based on timestamp and user
    job_id = f"uploads/{int(time.time())}-{user.split('@')[0]}.jpg"

    presigned = s3_client.generate_presigned_post(
        Bucket=os.environ["BUCKET_NAME"],
        Key=job_id,
        Fields={
            "x-amz-meta-user": user,
            "Content-Type": "image/jpeg",
        },
        Conditions=[
            {"x-amz-meta-user": user},
            ["starts-with", "$Content-Type", "image/"],
        ],
        ExpiresIn=3600,
    )
    return {"presigned": presigned, "job_id": job_id}

@router.get("/status")
def get_scan_status() -> Dict[str, Any]:
    """Polled by the frontend to check if LPR is complete."""
    job_id = router.current_event.query_string_parameters.get("job_id")
    if not job_id:
        # Return 400 if missing
        return Response( status_code=400, content_type="application/json",
                         body=json.dumps({"error": "Missing job_id"}) )
    logger.info("get_scan_status(%s)",job_id)

    user = router.context.get("user_email")
    try:
        response = table.get_item( Key={ "user_email": user, "sk": f"job#{job_id}", } )
    except table.exceptions.ResourceNotFoundException:
        logger.exception("unknown table %s",table)

    item = response.get("Item")
    if not item:
        return {"status": "pending"}

    return {"status": "complete", "data": item}


def _format_result_display(result) -> str:
    """Convert result (Brivo user dict or None) to display string."""
    if result is None:
        return "Not found"
    if isinstance(result, dict):
        first = result.get("firstName") or ""
        last = result.get("lastName") or ""
        return f"{first} {last}".strip() or "Unknown"
    return str(result)


@router.get("/history")
def get_user_history() -> list:
    """Return scans for the logged-in user. Past 30 days by default; show_all=1 for all."""
    user = router.context.get("user_email")
    if not user:
        return []
    show_all = router.current_event.query_string_parameters.get("show_all") == "1"
    limit = 500 if show_all else 50
    resp = table.query(
        KeyConditionExpression=Key("user_email").eq(user),
        ScanIndexForward=False,
        Limit=limit,
    )
    items = resp.get("Items", [])
    cutoff = int(time.time()) - (30 * 24 * 3600)  # 30 days ago
    if not show_all:
        items = [it for it in items if it.get("timestamp", 0) >= cutoff]
    # Add display-friendly fields and status; keep raw for future OCR/top_matches
    for it in items:
        it["result_display"] = _format_result_display(it.get("result"))
        it["plate_display"] = it.get("plate") or "—"
        it["status"] = "manual" if it.get("image_key") == "manual" else "complete"
    return items


@router.delete("/scan")
def delete_scan() -> Dict[str, Any]:
    """Delete a scan: DynamoDB entry and S3 object (if present). Authenticated."""
    user = router.context.get("user_email")
    if not user:
        raise ValueError("user_email not found in context")
    sk = router.current_event.query_string_parameters.get("sk")
    if not sk:
        return Response(
            status_code=400,
            content_type="application/json",
            body=json.dumps({"error": "Missing sk"}),
        )
    if not sk.startswith("job#"):
        return Response(
            status_code=400,
            content_type="application/json",
            body=json.dumps({"error": "Invalid sk"}),
        )
    resp = table.get_item(Key={"user_email": user, "sk": sk})
    item = resp.get("Item")
    if not item:
        return Response(
            status_code=404,
            content_type="application/json",
            body=json.dumps({"error": "Not found"}),
        )
    image_key = item.get("image_key")
    bucket = os.environ["BUCKET_NAME"]
    if image_key and image_key != "manual":
        try:
            s3_client.delete_object(Bucket=bucket, Key=image_key)
            logger.info("Deleted S3 object: bucket=%s key=%s user=%s", bucket, image_key, user)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("S3 delete failed (object may not exist): %s", exc)
    table.delete_item(Key={"user_email": user, "sk": sk})
    logger.info("Deleted scan: user=%s sk=%s image_key=%s", user, sk, image_key)
    return {"deleted": True}


@router.get("/image-url")
def get_image_presigned_url() -> Dict[str, Any]:
    """Return presigned GET URL for a scan image. User must own the scan."""
    user = router.context.get("user_email")
    if not user:
        raise ValueError("user_email not found in context")
    key = router.current_event.query_string_parameters.get("key")
    if not key:
        return Response(
            status_code=400,
            content_type="application/json",
            body=json.dumps({"error": "Missing key"}),
        )
    if key == "manual":
        return Response(
            status_code=404,
            content_type="application/json",
            body=json.dumps({"error": "No image for manual entry"}),
        )
    resp = table.get_item(Key={"user_email": user, "sk": f"job#{key}"})
    if not resp.get("Item"):
        return Response(
            status_code=404,
            content_type="application/json",
            body=json.dumps({"error": "Not found"}),
        )
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": os.environ["BUCKET_NAME"], "Key": key},
        ExpiresIn=3600,
    )
    return {"url": url}


@router.get("/all-plates")
def get_all_plates_api() -> list:
    """Return all plates from the Brivo database as [{plate, name}, ...]. Authenticated."""
    user = router.context.get("user_email")
    if not user:
        raise ValueError("user_email not found in context - authentication failed")
    return get_all_plates()


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
    except Exception:         # pylint: disable=broad-exception-caught
        region = table.meta.client.meta.region_name
        logger.info(f"DEBUG: Lambda Region={os.environ.get('AWS_REGION')} | Table Region={region} Name={table.name}")
        logger.exception("table %s put_item failed",table)

    return {"status": "complete", "data": item}

def rekognize(bucket=None, key=None,image_bytes=None) -> dict:
    """recognize an image that is provided or uploaded"""
    if os.environ.get('AWS_REGION','')=='local':
        return []
    if bucket is not None:
        image = {"S3Object" : {"Bucket":bucket, "Name":key}}
    elif image_bytes is not None:
        image = {"Bytes":image_bytes}
    else:
        raise RuntimeError("no image provided")

    rekognition = boto3.client("rekognition")
    rek_resp = rekognition.detect_text( Image=image )
    logger.debug("%s",json.dumps(rek_resp,indent=4,default=str))
    return [
        {"text": r["DetectedText"], "confidence": r["Confidence"]}
        for r in rek_resp["TextDetections"]
        if (r["Type"] in ("WORD", "LINE") and r["Confidence"] > 40)
        and len(r["DetectedText"]) >= 4
    ]

# --- Background Event Handling ---
def handle_s3_event(detail: Dict[str, Any]) -> None:
    """
    Triggered by S3 EventBridge...

    Performs LPR, Brivo lookup, and saves to DynamoDB.
    """
    bucket = detail["bucket"]["name"]
    key = detail["object"]["key"]
    plate = None
    result_name = None
    raw_ocr = None
    ocr_pct = None
    match_pct = None

    try:
        # 1. Retrieve metadata stored during the presigned post
        logger.info("s3_client=%s bucket=%s key=%s",s3_client,bucket,key)
        head = s3_client.head_object(Bucket=bucket, Key=key)
        user = head["Metadata"].get("user")

        ocr_results = rekognize(bucket=bucket, key=key)

        # 1. Try exact match first
        for det in ocr_results:
            text = det["text"] if isinstance(det, dict) else det
            conf = det.get("confidence", 0) if isinstance(det, dict) else 0
            result_name = brivo.brivo_lookup(text)
            if result_name:
                plate = text
                raw_ocr = text
                ocr_pct = round(conf, 1) if conf else None
                match_pct = 100.0  # exact match
                break

        # 2. Failing that, fuzzy match against all plates in DB
        if plate is None and ocr_results:
            plate_strings = [p["plate"] for p in get_all_plates()]
            if plate_strings:
                best = None
                for det in ocr_results:
                    text = det["text"] if isinstance(det, dict) else det
                    conf = det.get("confidence", 0) if isinstance(det, dict) else 0
                    fm = fuzzy_match.find_closest_plate_entry(
                        plate_strings, text, conf, min_score_thresh=60
                    )
                    if fm["matched_record"]:
                        if best is None:
                            best = fm
                            raw_ocr = text
                        elif fm["composite_score"] > best["composite_score"]:
                            best = fm
                            raw_ocr = text
                if best:
                    plate = best["matched_record"]
                    result_name = brivo.brivo_lookup(plate)
                    raw_ocr = raw_ocr or plate
                    ocr_pct = round(best["aws_confidence"], 1)
                    match_pct = round(best["match_score"], 1)

    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Background processing failed for %s: %s", key, exc)

    try:
        # 4. Save record for frontend polling and history
        item = {
            "user_email": user,
            "sk": f"job#{key}",
            "plate": plate,
            "result": result_name,
            "timestamp": int(time.time()),
            "image_key": key,
        }
        if raw_ocr is not None:
            item["ocr_text"] = raw_ocr
        if ocr_pct is not None:
            item["ocr_pct"] = ocr_pct
        if match_pct is not None:
            item["match_pct"] = match_pct
        table.put_item(Item=item)
        logger.info("Asynchronous scan complete for %s", key)
        logger.error("item=%s",json.dumps(item))
    except Exception:  # pylint: disable=broad-except
        region = table.meta.client.meta.region_name
        logger.info(f"DEBUG: Lambda Region={os.environ.get('AWS_REGION')} | Table Region={region} Name={table.name}")
        logger.exception("table %s put_item failed",table)

def main():
    parser = argparse.ArgumentParser(description='License Plate CLI tester',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--rekognize", help="analyze a file with rekognition" , type=Path)
    parser.add_argument("--store-plates", help='store all the plates in the DynamoDB database', action='store_true')
    parser.add_argument("--store-file", help="When storing all plates, also write the JSON to this file",type=Path)
    parser.add_argument("--search", help="try to match a plate to the database")
    args = parser.parse_args()
    if args.rekognize:
        print(json.dumps(rekognize(image_bytes=args.rekognize.read_bytes()),indent=4,default=str))
    if args.store_plates:
        save_all_plates(verbose=True,store_file=args.store_file)
    if args.search:
        print(get_all_plates())

if __name__=="__main__":
    main()
