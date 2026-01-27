# Actionable Suggestions for CarScan Project

## Priority 1: Critical Fixes (Must Fix Before Deployment)

### 1. Fix Import Errors in `brivo.py`
**File**: `src/app/brivo.py`

**Current Code**:
```python
import urllib.request
import json

def brivo_lookup(plate, state):
    # Uses: os, parameters, logger, urllib.parse - but not imported
```

**Fix**:
```python
import os
import urllib.request
import urllib.parse
import json
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities import parameters

logger = Logger(child=True)

def brivo_lookup(plate, state):
    # ... rest of code
```

### 2. Fix File Path in `main.py`
**File**: `src/app/main.py:41`

**Current**:
```python
with open("camera.html", "r", encoding="utf-8") as f:
```

**Fix**:
```python
import os
from pathlib import Path

# In serve_index():
template_path = Path(__file__).parent / "templates" / "camera.html"
with open(template_path, "r", encoding="utf-8") as f:
    return Response(status_code=200, content_type=content_types.TEXT_HTML, body=f.read())
```

### 3. Add DynamoDB Table Resource to `template.yaml`
**File**: `template.yaml`

**Add after line 80**:
```yaml
  MyTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: cala-garage-scans
      AttributeDefinitions:
        - AttributeName: user_email
          AttributeType: S
        - AttributeName: sk
          AttributeType: S
      KeySchema:
        - AttributeName: user_email
          KeyType: HASH
        - AttributeName: sk
          KeyType: RANGE
      BillingMode: PAY_PER_REQUEST
```

### 4. Fix Parameter References in `template.yaml`
**File**: `template.yaml:23-24`

**Current**:
```yaml
GOOGLE_SECRET_ARN: !Ref GoogleClientSecretArt  # Typo
BRIVO_SECRET_ARN: !Ref BrivoApiKeyArn
```

**Fix**:
```yaml
GOOGLE_SECRET_ARN: !Ref GoogleClientSecretArn  # Fix typo
BRIVO_SECRET_ARN: !Ref BrivoApiKeyArn  # This is correct
```

### 5. Fix Test Imports
**File**: `tests/test_carscan.py:5`

**Current**:
```python
from main import lambda_handler
```

**Fix**:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from app.main import lambda_handler
```

### 6. Add boto3 to requirements.txt
**File**: `src/app/requirements.txt`

**Add**:
```
boto3>=1.35.0
```

### 6b. Fix DynamoDB Key Condition Import
**File**: `src/app/carscan.py:69`

**Current**:
```python
KeyConditionExpression=boto3.dynamodb.conditions.Key('user_email').eq(user),
```

**Problem**: Incorrect import path for Key condition

**Fix**:
```python
from boto3.dynamodb.conditions import Key

# Then use:
KeyConditionExpression=Key('user_email').eq(user),
```

## Priority 2: Security Fixes

### 7. Implement Proper Authentication
**Current**: Simple cookie parsing

**Recommendation**: Choose one approach:

**Option A: Implement Google OAuth**
- Add OAuth routes in `main.py`
- Store session tokens in DynamoDB or use JWT
- Validate tokens on each request

**Option B: Session Management**
- Generate secure session tokens
- Store in DynamoDB with expiration
- Validate on each request

**Example Session Validation**:
```python
def check_auth(app_instance: APIGatewayHttpResolver, next_middleware):
    headers = app_instance.current_event.headers
    cookie = headers.get("Cookie", "") or headers.get("cookie", "")
    
    if "user_session=" not in cookie:
        return Response(status_code=401, body='{"error":"Unauthorized"}')
    
    try:
        session_token = cookie.split("user_session=")[1].split(";")[0]
        # Validate session token against DynamoDB
        # Check expiration
        # Extract email from validated session
        email = validate_session(session_token)
        if not email:
            return Response(status_code=401, body='{"error":"Invalid Session"}')
        app_instance.append_context(user_email=email)
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return Response(status_code=401, body='{"error":"Invalid Session"}')
    
    return next_middleware(app_instance)
```

### 8. Add Input Validation
**File**: `src/app/carscan.py`

**Add validation functions**:
```python
import re

VALID_STATES = {'MA', 'VA', 'NY', 'MD', 'DC', 'CA', 'TX', 'FL'}  # Add all states

def validate_plate(plate: str) -> bool:
    """Validate license plate format (basic check)"""
    if not plate or len(plate) > 10:
        return False
    # Remove spaces and hyphens for validation
    cleaned = re.sub(r'[\s\-]', '', plate.upper())
    # Alphanumeric only
    return bool(re.match(r'^[A-Z0-9]+$', cleaned))

def validate_state(state: str) -> bool:
    """Validate state code"""
    return state.upper() in VALID_STATES

# Use in manual_entry():
plate = body.get('plate', '').upper().strip()
state = body.get('state', 'MA').upper()

if not validate_plate(plate):
    return Response(status_code=400, body='{"error":"Invalid plate format"}')
if not validate_state(state):
    return Response(status_code=400, body='{"error":"Invalid state code"}')
```

### 9. Restrict CORS Origins
**File**: `template.yaml:95-97`

**Current**:
```yaml
CorsConfiguration:
  AllowOrigins: ['*']
```

**Fix**:
```yaml
CorsConfiguration:
  AllowOrigins:
    - 'https://v1.cybersecurity-policy.org'
    - 'https://v2.cybersecurity-policy.org'
  AllowMethods: ['GET','POST','OPTIONS']
  AllowHeaders: ['Content-Type', 'Cookie']
```

## Priority 3: Code Quality Improvements

### 10. Remove Hardcoded Test Logic
**File**: `src/app/carscan.py:88`

**Remove**:
```python
result_name = "Authorized User" if plate == "ABC1234" else "Unknown"
```

**Replace with**:
```python
result_name = brivo.brivo_lookup(plate, state)
```

### 11. Extract Constants
**File**: `src/app/carscan.py` (create constants section at top)

```python
# Constants
REKOGNITION_CONFIDENCE_THRESHOLD = 90
HISTORY_LIMIT = 50
PRESIGNED_URL_EXPIRY = 3600
POLLING_TIMEOUT = 30
POLLING_INTERVAL = 1.5
BRIVO_API_TIMEOUT = 5
```

**Use throughout code**:
```python
plate = next((t['DetectedText'] for t in rek_resp['TextDetections'] 
              if t['Confidence'] > REKOGNITION_CONFIDENCE_THRESHOLD), "NOT_FOUND")
```

### 12. Improve Error Handling
**File**: `src/app/carscan.py`

**Create error response helper**:
```python
def error_response(status_code: int, message: str) -> dict:
    """Standardized error response"""
    return {
        "statusCode": status_code,
        "body": json.dumps({"error": message}),
        "headers": {"Content-Type": "application/json"}
    }
```

**Use specific exceptions**:
```python
try:
    # code
except ClientError as e:
    logger.error(f"AWS service error: {e}")
    return error_response(500, "Service temporarily unavailable")
except Exception as e:
    logger.exception(f"Unexpected error: {e}")
    return error_response(500, "Internal server error")
```

### 13. Add Type Hints
**Example for `carscan.py`**:
```python
from typing import Dict, Any, Optional
from aws_lambda_powertools.event_handler import Router

def get_upload_params() -> Dict[str, Any]:
    """Generates a presigned URL with user identity in metadata."""
    user: str = router.context.get("user_email")
    state: str = router.current_event.query_string_parameters.get("state", "MA")
    # ...
```

### 14. Add Comprehensive Docstrings
**Example**:
```python
def get_upload_params() -> Dict[str, Any]:
    """
    Generates a presigned S3 POST URL for direct client uploads.
    
    The URL includes metadata fields (user email, state) that will be
    available to the async S3 event handler for processing.
    
    Returns:
        Dict containing:
            - presigned: S3 presigned POST data (url, fields)
            - job_id: Unique identifier for this upload job
            
    Raises:
        Response: 401 if user not authenticated
        Response: 500 if S3 operation fails
    """
```

## Priority 4: Infrastructure Improvements

### 15. Add S3 Lifecycle Policy
**File**: `template.yaml:74-80`

**Add to UploadBucket**:
```yaml
UploadBucket:
  Type: AWS::S3::Bucket
  Properties:
    NotificationConfiguration:
      EventBridgeConfiguration:
        EventBridgeEnabled: true
    LifecycleConfiguration:
      Rules:
        - Id: DeleteOldImages
          Status: Enabled
          ExpirationInDays: 90  # Adjust as needed
```

### 16. Add Dead Letter Queue
**File**: `template.yaml`

**Add after MyTable**:
```yaml
  S3EventDLQ:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub "carscan-s3-dlq-${StageName}"
      MessageRetentionPeriod: 1209600  # 14 days

# Add to MyWebFunction:
  DeadLetterQueue:
    Type: SQS
    TargetArn: !GetAtt S3EventDLQ.Arn
```

### 17. Add CloudWatch Alarms
**File**: `template.yaml` (add new section)

```yaml
  RekognitionErrorAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "carscan-rekognition-errors-${StageName}"
      MetricName: Errors
      Namespace: AWS/Lambda
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 5
      ComparisonOperator: GreaterThanThreshold
      Dimensions:
        - Name: FunctionName
          Value: !Ref MyWebFunction
```

### 18. Standardize Python Version
**Decision**: Use Python 3.13 (newer) or 3.11 (LTS)

**Action**: 
- Update `template-gemini.yaml` to match `template.yaml`
- Or deprecate `template-gemini.yaml` if not needed

## Priority 5: Frontend Improvements

### 19. Add Loading States
**File**: `src/app/static/camera.js`

**Add**:
```javascript
function setLoading(isLoading) {
    const btn = document.getElementById('scan-btn');
    btn.disabled = isLoading;
    btn.textContent = isLoading ? 'Processing...' : 'SCAN CAMERA';
}

// Use in captureAndScan():
setLoading(true);
try {
    // ... upload logic
} finally {
    setLoading(false);
}
```

### 20. Improve Error Handling
**File**: `src/app/static/camera.js`

**Add**:
```javascript
async function apiCall(endpoint, options={}) {
    try {
        const cleanEndpoint = endpoint.startsWith('/') ? endpoint.substring(1) : endpoint;
        const url = `${window.location.origin}${API_BASE}${cleanEndpoint}`;
        const response = await fetch(url, options);
        
        if (!response.ok) {
            const error = await response.json().catch(() => ({error: 'Unknown error'}));
            throw new Error(error.error || `HTTP ${response.status}`);
        }
        
        return response;
    } catch (error) {
        console.error('API call failed:', error);
        showResult("Error", error.message, false);
        throw error;
    }
}
```

### 21. Implement Exponential Backoff for Polling
**File**: `src/app/static/camera.js`

**Replace polling function**:
```javascript
async function pollForResult(jobId) {
    showResult("Processing...", "Analyzing Image...", null);
    
    let attempts = 0;
    const maxAttempts = 20;
    let delay = 1000; // Start with 1 second
    
    const poll = async () => {
        if (attempts >= maxAttempts) {
            showResult("Timeout", "Scan took too long. Please try again.", false);
            return;
        }
        
        try {
            const statusRes = await apiCall(`api/status/${encodeURIComponent(jobId)}`);
            const statusData = await statusRes.json();
            
            if (statusData.status === 'complete') {
                const data = statusData.data;
                const found = data.result !== "Unknown" && data.plate !== "NOT_FOUND";
                showResult(data.result, `${data.plate} (${data.state})`, found);
                return;
            }
            
            // Exponential backoff: 1s, 2s, 4s, 8s, then cap at 5s
            attempts++;
            delay = Math.min(delay * 1.5, 5000);
            setTimeout(poll, delay);
        } catch (e) {
            console.error("Polling error", e);
            attempts++;
            setTimeout(poll, delay);
        }
    };
    
    poll();
}
```

## Priority 6: Testing

### 22. Fix and Expand Tests
**File**: `tests/test_carscan.py`

**Complete rewrite with proper structure**:
```python
import pytest
import json
from unittest.mock import MagicMock, patch, Mock
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from app.main import lambda_handler, check_auth
from app.carscan import get_upload_params, handle_s3_event
from app.brivo import brivo_lookup

# ... rest of comprehensive tests
```

### 23. Add Integration Tests
**Create**: `tests/test_integration.py`

Test full flows:
- Upload → S3 → Processing → DynamoDB
- Manual entry → Brivo → DynamoDB
- Authentication flow

## Additional Recommendations

### 24. Add API Documentation
**Create**: `API.md`

Document all endpoints:
- Request/response formats
- Error codes
- Authentication requirements
- Rate limits

### 25. Add Deployment Guide
**Create**: `DEPLOYMENT.md`

Include:
- Prerequisites
- Environment setup
- Deployment steps
- Troubleshooting
- Rollback procedures

### 26. Add Development Setup Guide
**Create**: `DEVELOPMENT.md`

Include:
- Local environment setup
- Running tests
- Local DynamoDB/S3 setup
- Debugging tips

### 27. Consider Adding Request ID Tracking
**Add to all responses**:
```python
import uuid

request_id = str(uuid.uuid4())
logger.append_keys(request_id=request_id)
# Include in response headers for debugging
```

### 28. Add Health Check Endpoint
**File**: `src/app/main.py`

```python
@app.get("/health")
def health_check():
    """Health check endpoint for load balancers"""
    return {"status": "healthy", "service": "carscan"}
```

## Implementation Order

1. **Week 1**: Fix all critical issues (Priority 1)
2. **Week 2**: Implement security fixes (Priority 2)
3. **Week 3**: Code quality improvements (Priority 3)
4. **Week 4**: Infrastructure and testing (Priority 4-6)

## Success Criteria

- All tests pass
- No import errors
- Successful SAM deployment
- Security review passed
- Code coverage > 80%
- Documentation complete
