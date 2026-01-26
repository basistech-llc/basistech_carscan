import os
import json
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from carscan import router as carscan_router

logger = Logger(service="CarScanMain")
app = APIGatewayHttpResolver()

# Global Auth Middleware
def check_auth(app_instance: APIGatewayHttpResolver, next_middleware):
    cookie = app_instance.current_event.headers.get("Cookie", "") or \
             app_instance.current_event.headers.get("cookie", "")
    if "user_session=" not in cookie:
        return Response(status_code=401, content_type=content_types.APPLICATION_JSON, body='{"error":"Unauthorized"}')

    email = cookie.split("user_session=")[1].split(";")[0]
    app_instance.append_context(user_email=email)
    return next_middleware(app_instance)

# Register business logic with auth protection
app.include_router(carscan_router, prefix="/api")
carscan_router.use(middlewares=[check_auth])

@app.get("/")
def index():
    try:
        with open("camera.html", "r") as f:
            return Response(status_code=200, content_type=content_types.TEXT_HTML, body=f.read())
    except Exception: # pylint: disable=broad-except
        return Response(status_code=404, body="Not Found")

def lambda_handler(event, context):
    # Route S3 EventBridge signals
    if event.get("source") == "aws.s3":
        return handle_s3_event(event.get("detail"))

    # Route Keep-alive warmers
    if event.get("source") == "aws.events":
        return {"warmed": True}

    return app.resolve(event, context)
