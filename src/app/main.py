"""Lambda entrypoint and global routing setup for CarScan."""

import os
import logging
import time
import json
from pathlib import Path
from typing import Any, Callable, Dict

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from .carscan import router as carscan_router, handle_s3_event
from . import oidc as oidc_module

logger = Logger(service="APP") # Automatically picks up LOG_LEVEL from env
logger.setLevel(logging.INFO)
app = APIGatewayHttpResolver(enable_validation=False)
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
jinja_env = Environment(loader=FileSystemLoader(template_dir))

def _get_email_from_cookie(headers: Dict[str, str]) -> str | None:
    """Extract user email from user_session cookie, or return None."""
    cookie = (headers.get("Cookie") or headers.get("cookie") or "").strip()
    if "user_session=" not in cookie:
        return None
    try:
        return cookie.split("user_session=")[1].split(";")[0].strip()
    except Exception:  # pylint: disable=broad-except
        return None


def check_auth(app_instance: APIGatewayHttpResolver, next_middleware: Callable) -> Response:
    """Verify user_session cookie (set after OIDC login) and inject email into context."""
    email = _get_email_from_cookie(app_instance.current_event.headers)
    if not email:
        return Response(
            status_code=401,
            content_type=content_types.APPLICATION_JSON,
            body=json.dumps({"error": "Unauthorized", "t": time.time()}),
        )
    app_instance.append_context(user_email=email)
    if hasattr(app_instance, "context") and isinstance(app_instance.context, dict):
        app_instance.context["user_email"] = email
    return next_middleware(app_instance)


def conditional_api_auth(app_instance: APIGatewayHttpResolver, next_middleware: Callable) -> Response:
    """Run check_auth only for /api/* so that include_router does not apply auth globally."""
    path = getattr(app_instance.current_event, "path", None) or getattr(
        app_instance.current_event, "raw_path", ""
    ) or "/"
    if path.startswith("/api"):
        return check_auth(app_instance, next_middleware)
    return next_middleware(app_instance)


# Apply auth only for /api/* (include_router merges router middlewares into app globally)
app.use(middlewares=[conditional_api_auth])
app.include_router(carscan_router, prefix="/api")


@app.get("/auth/login")
def auth_login() -> Response:
    """Redirect to Google OIDC authorization URL."""
    try:
        config = oidc_module.get_oidc_config()
        auth_url, _ = oidc_module.build_oidc_authorization_url_stateless(openid_config=config)
        return Response(
            status_code=302,
            headers={"Location": auth_url},
            body="",
        )
    except Exception:  # pylint: disable=broad-except
        logger.exception("OIDC login failed")
        return Response(
            status_code=503,
            content_type=content_types.APPLICATION_JSON,
            body='{"error":"Login temporarily unavailable"}',
        )


@app.get("/auth/callback")
def auth_callback() -> Response:
    """Handle OIDC redirect: exchange code for tokens, set session cookie, redirect to /."""
    params = app.current_event.query_string_parameters or {}
    # API Gateway may send multi-value params as lists; use first value
    callback_params = {}
    for k, v in params.items():
        callback_params[k] = v[0] if isinstance(v, list) else v
    try:
        config = oidc_module.get_oidc_config()
        result = oidc_module.handle_oidc_redirect_stateless(
            openid_config=config,
            callback_params=callback_params,
        )
        email = (result.get("claims") or {}).get("email")
        if not email:
            return Response(status_code=400, body="Missing email in IdP response")
        # Set cookie and redirect to app root
        cookie_value = f"user_session={email}; Path=/; HttpOnly; SameSite=Lax"
        return Response(
            status_code=302,
            headers={"Location": "/", "Set-Cookie": cookie_value},
            body="",
        )
    except Exception:  # pylint: disable=broad-except
        logger.exception("OIDC callback failed")
        return Response(
            status_code=400,
            content_type=content_types.APPLICATION_JSON,
            body='{"error":"Login failed"}',
        )


@app.get("/hello")
def hello() -> dict:
    return {"message": "Hello world!"}

@app.get("/hello/<name>")
def hello_name(name):
    logger.info(f"Request from {name} received")
    return {"message": f"hello {name}!"}



@app.get("/")
def serve_index() -> Response:
    """Serve landing page if unauthenticated, otherwise the camera app."""
    if _get_email_from_cookie(app.current_event.headers):
        template_name = "camera.html"
    else:
        template_name = "landing.html"
    try:
        template_path = Path(__file__).parent / "templates" / template_name
        with template_path.open("r", encoding="utf-8") as f:
            return Response(
                status_code=200,
                content_type=content_types.TEXT_HTML,
                body=f.read(),
            )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to load template %s: %s", template_name, exc)
        return Response(status_code=404, body="Not Found")

def render_dynamic_template(template_name: str) -> Response:
    """Helper to find a template, inject query params, and return a Response."""
    # Extract query parameters to pass to the template automatically
    # Example: ?name=Bob becomes {{ name }} in the template
    query_params = app.current_event.query_string_parameters or {}

    try:
        template = jinja_env.get_template(template_name)
        html = template.render(**query_params, path_name=template_name)
        return Response(
            status_code=200,
            content_type=content_types.TEXT_HTML,
            body=html
        )
    except TemplateNotFound:
        logger.warning(f"Template not found: {template_name}")
        return Response(
            status_code=404,
            body="404 - Page Not Found",
            content_type=content_types.TEXT_PLAIN
        )



@app.get("/<proxy+>")
def catch_all_templates(proxy):
    """
    Greedy route that catches any other path and tries to find
    a matching .html file in the templates folder.
    """
    logger.info("catch_all_templates(%s)", proxy)
    return render_dynamic_template(proxy)

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
