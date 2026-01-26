import os
import time
import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import Router
from aws_lambda_powertools.utilities import parameters

logger = Logger(child=True)
router = Router()

s3 = boto3.client('s3')
rekognition = boto3.client('rekognition')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['TABLE_NAME'])

@router.get("/upload-url")
def get_upload_url():
    user = router.context.get("authenticated_user")
    key = f"uploads/{user}/{int(time.time())}.jpg"
    
    presigned = s3.generate_presigned_post(
        Bucket=os.environ['BUCKET_NAME'],
        Key=key,
        Fields={"acl": "public-read", "Content-Type": "image/jpeg"},
        ExpiresIn=3600
    )
    return presigned

@router.post("/scan")
def process_car_scan():
    user = router.context.get("authenticated_user")
    body = router.current_event.json_body
    
    # Fetch Brivo API Key via Powertools (cached for efficiency)
    brivo_secrets = parameters.get_secret(os.environ['BRIVO_SECRET_ARN'], transform='json')
    
    image_key = body.get('image_key')
    plate_text = body.get('manual_text')
    
    if image_key and not plate_text:
        plate_text = _call_rekognition(image_key)

    # Business Logic and Logging
    log_entry = {
        'user_email': user,
        'sk': f"log#{int(time.time())}",
        'plate': plate_text or "UNKNOWN"
    }
    table.put_item(Item=log_entry)
    
    return {"status": "success", "plate": plate_text}

def _call_rekognition(key):
    try:
        resp = rekognition.detect_text(Image={'S3Object': {'Bucket': os.environ['BUCKET_NAME'], 'Name': key}})
        return next((t['DetectedText'] for t in resp['TextDetections'] if t['Confidence'] > 90), None)
    except Exception: # pylint: disable=broad-except
        logger.error(f"Rekognition failed for {key}")
        return None
