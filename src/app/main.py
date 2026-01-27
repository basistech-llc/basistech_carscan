from pathlib import Path

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from carscan import router as carscan_router, handle_s3_event

# Logger automatically picks up LOG_LEVEL from template.yaml globals
logger = Logger(service="CarScanMain")
app = APIGatewayHttpResolver()

# --- Middleware: Global Authentication ---
def check_auth(app_instance: APIGatewayHttpResolver, next_middleware):
    """Verifies user_session cookie and injects email into context."""
    headers = app_instance.current_event.headers
    cookie = headers.get("Cookie", "") or headers.get("cookie", "")
    
    if "user_session=" not in cookie:
        return Response(
            status_code=401, 
            content_type=content_types.APPLICATION_JSON, 
            body='{"error":"Unauthorized"}'
        )
    
    # Extract email for use in carscan.py logic
    try:
        email = cookie.split("user_session=")[1].split(";")[0]
        app_instance.append_context(user_email=email)
    except Exception: # pylint: disable=broad-except
        return Response(status_code=401, body="Invalid Session")
        
    return next_middleware(app_instance)

# Protect all routes inside the /api prefix
app.include_router(carscan_router, prefix="/api")
carscan_router.use(middlewares=[check_auth])

@app.get("/")
def serve_index():
    """Serves the main camera interface."""
    try:
        template_path = Path(__file__).parent / "templates" / "camera.html"
        with template_path.open("r", encoding="utf-8") as f:
            return Response(status_code=200, content_type=content_types.TEXT_HTML, body=f.read())
    except Exception as e: # pylint: disable=broad-except
        logger.error(f"Failed to load index: {e}")
        return Response(status_code=404, body="Index Not Found")

# --- Main Lambda Handler ---
def lambda_handler(event, context):
    # 1. Route S3 EventBridge Notifications (Asynchronous LPR)
    if event.get("source") == "aws.s3":
        logger.info("Processing S3 EventBridge trigger")
        return handle_s3_event(event.get("detail"))
    
    # 2. Route EventBridge Heartbeats (Keep-warm)
    if event.get("source") == "aws.events":
        return {"warmed": True}

    # 3. Route HTTP API Calls via Powertools Resolver
    return app.resolve(event, context)
