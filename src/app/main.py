import json
import os
import boto3
import uuid
import time
import urllib.request
import urllib.parse
import base64
from http import cookies

# --- AWS Clients ---
s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
rekognition = boto3.client('rekognition')
secrets_client = boto3.client('secretsmanager')

table = dynamodb.Table(os.environ['TABLE_NAME'])
bucket_name = os.environ['BUCKET_NAME']

# --- Secrets Management ---
def get_secret(secret_arn):
    """
    Fetches and parses a JSON secret from Secrets Manager.
    """
    try:
        get_secret_value_response = secrets_client.get_secret_value(SecretId=secret_arn)
        if 'SecretString' in get_secret_value_response:
            return json.loads(get_secret_value_response['SecretString'])
    except Exception as e:
        print(f"Error retrieving secret {secret_arn}: {e}")
        return {}
    return {}

# Load secrets during Cold Start
print("Loading Secrets...")
google_creds = get_secret(os.environ['GOOGLE_SECRET_ARN'])
brivo_creds = get_secret(os.environ['BRIVO_SECRET_ARN'])

GOOGLE_CLIENT_ID = google_creds.get('client_id')
GOOGLE_CLIENT_SECRET = google_creds.get('client_secret')
BRIVO_API_KEY = brivo_creds.get('api_key')

def lambda_handler(event, context):
    path = event['path']
    method = event['httpMethod']
    headers = event.get('headers', {}) or {}
    
    # 1. Static File Serving
    if path == "/" or path == "/index.html":
        user = get_authenticated_user(headers)
        if not user:
             return serve_login_page()
        return serve_file('index.html', 'text/html')
    
    if path.startswith("/static/"):
        filename = path.split("/")[-1]
        ext = filename.split(".")[-1]
        mime = "text/css" if ext == "css" else "application/javascript"
        # Security: Prevent directory traversal
        if "/" in filename or ".." in filename: return json_response({}, 404)
        return serve_file(filename, mime)

    # 2. Authentication Flow (Google OAuth 2.0)
    if path == "/auth/login":
        return initiate_google_auth(event)
    
    if path == "/auth/callback":
        return handle_google_callback(event)

    # 3. API Routes (Protected)
    user = get_authenticated_user(headers)
    if not user:
        return json_response({'error': 'Unauthorized'}, 401)

    if path == "/api/upload-url" and method == "GET":
        key = f"uploads/{user}/{int(time.time())}.jpg"
        presigned = s3.generate_presigned_post(
            Bucket=bucket_name,
            Key=key,
            Fields={"acl": "public-read", "Content-Type": "image/jpeg"},
            Conditions=[{"acl": "public-read"}, {"Content-Type": "image/jpeg"}],
            ExpiresIn=3600
        )
        return json_response(presigned)

    if path == "/api/scan" and method == "POST":
        body = json.loads(event['body'])
        state = body.get('state', 'MA')
        
        plate_text = body.get('manual_text')
        image_key = body.get('image_key')
        
        # Perform Rekognition if image provided
        if image_key and not plate_text:
            plate_text = perform_lpr(image_key)
        
        if not plate_text:
            return json_response({'error': 'No plate detected', 'found': False}, 200)

        clean_plate = ''.join(c for c in plate_text if c.isalnum()).upper()
        
        # Brivo Lookup
        brivo_result = lookup_brivo_user(clean_plate, state)
        
        # Log to DynamoDB
        save_log(user, image_key, clean_plate, state, brivo_result)
        
        return json_response({
            'plate': clean_plate,
            'state': state,
            'found': bool(brivo_result),
            'user': brivo_result or "Not Found"
        })

    if path == "/api/history" and method == "GET":
        resp = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('user_email').eq(user),
            ScanIndexForward=False, # Newest first
            Limit=50
        )
        return json_response(resp.get('Items', []))

    return json_response({'error': 'Not Found'}, 404)

# --- Helpers ---

def serve_file(filename, mime):
    try:
        with open(filename, 'r') as f:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": mime},
                "body": f.read()
            }
    except:
        return json_response({}, 404)

def serve_login_page():
    # Simple login page
    html = """
    <html><body style="font-family:sans-serif; text-align:center; padding-top:50px;">
        <h1>BasisTech Scan App</h1>
        <p>Please log in to continue.</p>
        <a href='/auth/login' style="background:#4285F4; color:white; padding:10px 20px; text-decoration:none; border-radius:5px;">Login with Google</a>
    </body></html>
    """
    return {"statusCode": 200, "headers": {"Content-Type": "text/html"}, "body": html}

def get_base_url(event):
    stage = event['requestContext']['stage']
    host = event['headers'].get('Host')
    proto = event['headers'].get('X-Forwarded-Proto', 'https')
    return f"{proto}://{host}/{stage}"

def initiate_google_auth(event):
    base_url = get_base_url(event)
    redirect_uri = f"{base_url}/auth/callback"
    
    scope = "openid email"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={GOOGLE_CLIENT_ID}&"
        f"response_type=code&"
        f"scope={scope}&"
        f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
        f"access_type=online"
    )
    return {
        "statusCode": 302,
        "headers": {"Location": auth_url},
        "body": ""
    }

def handle_google_callback(event):
    code = event.get('queryStringParameters', {}).get('code')
    if not code: return json_response({'error': 'No code'}, 400)
    
    base_url = get_base_url(event)
    redirect_uri = f"{base_url}/auth/callback"

    # Exchange code for token
    data = urllib.parse.urlencode({
        'code': code,
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code'
    }).encode()
    
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            token_resp = json.loads(res.read())
            id_token = token_resp.get('id_token')
            
            # Decode ID Token to get email (Validation skipped for MVP)
            parts = id_token.split('.')
            payload = json.loads(base64_url_decode(parts[1]))
            email = payload.get('email')
            
            return {
                "statusCode": 302,
                "headers": {
                    "Location": "../",
                    "Set-Cookie": f"user_session={email}; Secure; HttpOnly; Path=/; Max-Age=86400"
                },
                "body": ""
            }
    except Exception as e:
        return json_response({'error': str(e)}, 500)

def get_authenticated_user(headers):
    cookie_header = headers.get('Cookie', '') or headers.get('cookie', '')
    if 'user_session=' in cookie_header:
        try:
            return cookie_header.split('user_session=')[1].split(';')[0]
        except:
            return None
    return None

def base64_url_decode(inp):
    padding = 4 - (len(inp) % 4)
    inp += ("=" * padding)
    return base64.urlsafe_b64decode(inp).decode()

def perform_lpr(key):
    try:
        response = rekognition.detect_text(
            Image={'S3Object': {'Bucket': bucket_name, 'Name': key}}
        )
        for text in response['TextDetections']:
            # Confidence Check 90%
            if text['Type'] == 'LINE' and text['Confidence'] >= 90.0:
                txt = text['DetectedText'].replace(" ", "")
                if len(txt) > 4 and txt.isalnum():
                    return txt
    except Exception as e:
        print(f"LPR Error: {e}")
    return None

def lookup_brivo_user(plate, state):
    # USE SECRETS HERE
    if not BRIVO_API_KEY:
        print("Brivo Secret missing")
        return None
        
    print(f"Using Brivo Key: {BRIVO_API_KEY[:4]}... to search {plate} in {state}")
    
    # Mock Database
    mock_db = {"ABC1234": "Simson Garfinkel", "TEST99": "Jane Doe"}
    return mock_db.get(plate)

def save_log(user, image_key, plate, state, result):
    table.put_item(Item={
        'user_email': user,
        'sk': f"log#{int(time.time())}",
        'image_key': image_key or "manual",
        'plate': plate,
        'state': state,
        'result': result or "Not Found"
    })

def json_response(data, code=200):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(data)
    }
