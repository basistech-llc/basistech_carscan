# CarScan Architecture Documentation

## Overview

CarScan is a serverless license plate recognition (LPR) application built on AWS Lambda. It allows users to scan license plates using their mobile device camera, performs optical character recognition (OCR) using AWS Rekognition, and looks up the plate owner in the Brivo access control system.

## System Architecture

### High-Level Flow

```
User (Mobile Browser)
    ↓
API Gateway HTTP API
    ↓
Lambda Function (main.py)
    ↓
├─→ Unauthenticated: GET / → landing page (Google Login)
├─→ GET /auth/login → redirect to Google OIDC
├─→ GET /auth/callback → exchange code, set user_session cookie, redirect to /
├─→ Authenticated: GET / → camera app (camera.html)
├─→ Authentication Middleware (user_session cookie, set after OIDC)
├─→ API Routes (carscan.py)
│   ├─→ /api/upload-url (presigned S3 URL)
│   ├─→ /api/status/<job_id> (polling endpoint)
│   ├─→ /api/history (user scan history)
│   └─→ /api/manual (manual plate entry)
│
└─→ S3 Upload Trigger (EventBridge)
    ↓
    Async LPR Processing
    ├─→ AWS Rekognition (text detection)
    ├─→ Brivo API (user lookup)
    └─→ DynamoDB (save result)
```

### Components

#### 1. Frontend (Client-Side)
- **Location**: `src/app/templates/camera.html`, `src/app/static/camera.js`
- **Technology**: Vanilla JavaScript, HTML5 Camera API
- **Functionality**:
  - Camera access and video stream
  - Image capture and upload to S3
  - Polling for scan results
  - Manual plate entry
  - Scan history display

#### 2. API Gateway & Lambda
- **Entry Point**: `src/app/main.py::lambda_handler`
- **Framework**: AWS Lambda Powertools (HTTP Resolver)
- **Routing**:
  - HTTP API requests → `APIGatewayHttpResolver`
  - S3 EventBridge events → `handle_s3_event()`
  - Scheduled events → heartbeat response

#### 3. Authentication (OIDC with Google)
- **Method**: Google OIDC provider; session stored in `user_session` cookie after login
- **Implementation**: `src/app/oidc.py` (discovery, auth URL, token exchange, ID token verification); `main.py` routes `/auth/login`, `/auth/callback` and `check_auth` middleware
- **Config**: JSON in AWS Secrets Manager at `GOOGLE_SECRET_ARN` (client_id, client_secret, redirect_uri; optional oidc_discovery_endpoint, defaults to Google)
- **Landing**: Unauthenticated users see `landing.html` with "Google Login" button; authenticated users see camera app at `/`

#### 4. API Routes (`carscan.py`)
- **Router**: AWS Lambda Powertools Router
- **Endpoints**:
  - `GET /api/upload-url`: Generates presigned S3 POST URL with user metadata
  - `GET /api/status/<job_id>`: Polls DynamoDB for scan completion
  - `GET /api/history`: Returns last 50 scans for authenticated user
  - `POST /api/manual`: Manually enter plate and perform Brivo lookup

#### 5. Asynchronous Processing
- **Trigger**: S3 EventBridge notification on object creation
- **Handler**: `handle_s3_event()` in `carscan.py`
- **Steps**:
  1. Retrieve S3 object metadata (user, state)
  2. Call AWS Rekognition `detect_text()` API
  3. Extract plate number from text detections
  4. Lookup user in Brivo API
  5. Save result to DynamoDB

#### 6. AWS Services

##### S3 (UploadBucket)
- **Purpose**: Store uploaded license plate images
- **Configuration**: EventBridge notifications enabled
- **Lifecycle**: Images stored with metadata (user email, state)

##### DynamoDB (cala-garage-scans)
- **Table Structure**:
  - Partition Key: `user_email` (String)
  - Sort Key: `sk` (String) - format: `job#<s3_key>` or `job#manual/<timestamp>`
- **Attributes**:
  - `plate`: Detected plate number
  - `state`: State code (MA, VA, NY, etc.)
  - `result`: Brivo lookup result (user name or "Unknown")
  - `timestamp`: Unix timestamp
  - `image_key`: S3 key or "manual"

##### AWS Rekognition
- **Service**: Text detection API
- **Usage**: `detect_text()` on uploaded images
- **Processing**: Filters text detections by confidence > 90%

##### Brivo API Integration
- **Module**: `src/app/brivo.py`
- **Function**: `brivo_lookup(plate, state)`
- **API**: `https://api.brivo.com/v1/api/users`
- **Method**: Query by custom field `customField_Plate`
- **Authentication**: API key from AWS Secrets Manager

### Data Flow

#### Image Upload Flow
1. User captures image via camera
2. Frontend requests presigned URL from `/api/upload-url`
3. Frontend uploads image directly to S3 with metadata
4. S3 triggers EventBridge notification
5. Lambda processes image asynchronously
6. Frontend polls `/api/status/<job_id>` until complete

#### Manual Entry Flow
1. User enters plate number manually
2. Frontend POSTs to `/api/manual`
3. Lambda performs Brivo lookup synchronously
4. Result saved to DynamoDB
5. Response returned immediately

### Infrastructure as Code

#### template.yaml (Primary)
- **Format**: AWS SAM (Serverless Application Model)
- **Runtime**: Python 3.13 (ARM64)
- **Features**:
  - Custom domain configuration
  - ACM certificate management
  - Environment-specific mappings (prod/stage)
  - EventBridge rules for S3 and scheduled events

#### template-gemini.yaml (Alternative)
- **Format**: AWS SAM
- **Runtime**: Python 3.9
- **Features**: Secrets Manager resource creation
- **Status**: Appears to be an earlier version

### Security

#### Current Implementation
- Google OIDC authentication; session cookie (`user_session`) set after successful login
- State parameter signed with HMAC (CSRF/replay); PKCE for code exchange
- Secrets (Google client, Brivo API key) in AWS Secrets Manager
- IAM policies restrict Lambda permissions

#### OIDC state: itsdangerous vs JWT
We use **itsdangerous.URLSafeTimedSerializer** for the OIDC `state` parameter (signed, time-limited) rather than JWT. Rationale: itsdangerous is purpose-built for this (dumps/loads with `max_age`), has clear exceptions (BadSignature, SignatureExpired), and keeps the code simple. Using JWT for state would remove one dependency but add more manual payload/options and less specific exceptions. Decision: keep itsdangerous unless we explicitly want to drop that dependency. See also `src/app/oidc.py` module docstring.

#### Remaining / Optional
- Session expiration (cookie is long-lived; consider Max-Age)
- CSRF tokens on state-changing API calls
- Rate limiting; input sanitization

### Dependencies

#### Python Packages (`requirements.txt`)
- `aws-lambda-powertools==3.24.0`: Framework for Lambda
- `boto3`: AWS SDK (implicit, not listed)
- Standard library: `urllib`, `json`, `os`, `time`

#### JavaScript
- Vanilla JavaScript (no frameworks)
- Browser APIs: `getUserMedia`, `fetch`, `FormData`

## Deployment

### AWS SAM Deployment
```bash
sam build
sam deploy --profile <profile-name>
```

### Environment Variables
- `LOG_LEVEL`: Logging verbosity
- `TABLE_NAME`: DynamoDB table name
- `BUCKET_NAME`: S3 bucket name
- `GOOGLE_SECRET_ARN`: Secrets Manager ARN for Google OIDC config JSON (client_id, client_secret, redirect_uri)
- `BRIVO_SECRET_ARN`: Secrets Manager ARN for Brivo API key

### Local Development
- Uses `Makefile.dev` for local DynamoDB and MinIO (S3 clone)
- SAM Local for API testing
- Poetry for Python dependency management

## Limitations & Known Issues

1. **Authentication**: Google OIDC is implemented; session cookie has no explicit expiration
2. **Error Handling**: Minimal error handling and user feedback
3. **Plate Detection**: Simple confidence threshold; no validation or formatting
4. **Brivo Integration**: Hardcoded test case (`ABC1234`) in manual entry
5. **Frontend**: No loading states, error messages, or retry logic
6. **Testing**: Test file has import errors and incomplete coverage
