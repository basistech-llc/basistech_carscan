"""Lambda entrypoint and global routing setup for CarScan."""

from pathlib import Path
from typing import Any, Callable, Dict

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import (
    APIGatewayHttpResolver,
    Response,
    content_types,
)

from .carscan import router as carscan_router, handle_s3_event

# Logger automatically picks up LOG_LEVEL from template.yaml globals
logger = Logger(service="CarScanMain")
app = APIGatewayHttpResolver()


def check_auth(
    app_instance: APIGatewayHttpResolver, next_middleware: Callable
) -> Response:
    """Verify user_session cookie and inject email into the Powertools context."""
    headers = app_instance.current_event.headers
    cookie = headers.get("Cookie", "") or headers.get("cookie", "")

    if "user_session=" not in cookie:
        return Response(
            status_code=401,
            content_type=content_types.APPLICATION_JSON,
            body='{"error":"Unauthorized"}',
        )

    try:
        email = cookie.split("user_session=")[1].split(";")[0]
        app_instance.append_context(user_email=email)
    except Exception:  # pylint: disable=broad-except
        return Response(status_code=401, body="Invalid Session")

    return next_middleware(app_instance)


# Protect all routes inside the /api prefix
app.include_router(carscan_router, prefix="/api")
carscan_router.use(middlewares=[check_auth])


@app.get("/")
def serve_index() -> Response:
    """Serve the main camera interface HTML page."""
    try:
        template_path = Path(__file__).parent / "templates" / "camera.html"
        with template_path.open("r", encoding="utf-8") as f:
            return Response(
                status_code=200,
                content_type=content_types.TEXT_HTML,
                body=f.read(),
            )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to load index: %s", exc)
        return Response(status_code=404, body="Index Not Found")


def lambda_handler(event: Dict[str, Any], context: Any) -> Any:
    """
    Main Lambda handler.

    Routes:
    - S3 EventBridge notifications for async LPR.
    - EventBridge heartbeats for keep-warm.
    - HTTP API calls via Powertools resolver.
    """
    if event.get("source") == "aws.s3":
        logger.info("Processing S3 EventBridge trigger")
        return handle_s3_event(event.get("detail"))

    if event.get("source") == "aws.events":
        return {"warmed": True}

    return app.resolve(event, context)

