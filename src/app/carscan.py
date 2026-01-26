import os
import time
import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import Router

logger = Logger(child=True)
router = Router()
s3_client = boto3.client('s3')
# ... other clients (rekognition, dynamodb) ...

@router.get("/upload-url")
def get_url():
    user = router.context.get("user_email")
    state = router.current_event.query_string_parameters.get("state", "MA")
    job_id = f"{int(time.time())}-{user.split('@')[0]}"
    key = f"uploads/{job_id}.jpg"
    
    # Passing metadata into the presigned POST
    presigned = s3_client.generate_presigned_post(
        Bucket=os.environ['BUCKET_NAME'],
        Key=key,
        Fields={
            "x-amz-meta-user": user,
            "x-amz-meta-state": state,
            "Content-Type": "image/jpeg"
        },
        Conditions=[
            {"x-amz-meta-user": user},
            {"x-amz-meta-state": state},
            ["starts-with", "$Content-Type", "image/"]
        ],
        ExpiresIn=3600
    )
    return {"presigned": presigned, "job_id": key}

# Dedicated function to handle the EventBridge logic
def handle_s3_event(detail):
    bucket = detail['bucket']['name']
    key = detail['object']['key']
    
    # 1. Fetch metadata from the uploaded object
    head = s3_client.head_object(Bucket=bucket, Key=key)
    user = head['Metadata'].get('user')
    state = head['Metadata'].get('state', 'MA')
    
    # 2. Perform LPR and Brivo Lookup
    plate = _do_lpr(bucket, key)
    result = _brivo_lookup(plate, state)
    
    # 3. Update DynamoDB (Polling Target)
    table.put_item(Item={
        'user_email': user,
        'sk': f"job#{key}", # job_id is the key
        'plate': plate or "NOT_DETECTED",
        'result': result or "Not Found",
        'status': 'COMPLETE',
        'timestamp': int(time.time())
    })
    logger.info(f"Scan complete for {key}")
