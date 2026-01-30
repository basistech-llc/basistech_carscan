"""Lambda entrypoint and global routing setup for CarScan."""

import os
import logging
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict
import mimetypes

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.shared.cookies import Cookie, SameSite
from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .carscan import router as carscan_router, handle_s3_event
from . import oidc as oidc_module

logger = Logger(service="APP")
# Respect LOG_LEVEL from env (e.g. DEBUG); do not override
_log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
logger.setLevel(getattr(logging, _log_level_name, logging.INFO))
app = APIGatewayHttpResolver(enable_validation=False)
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
jinja_env = Environment(loader=FileSystemLoader(template_dir))

# Session cookie signer. TODO: migrate secret to Amazon Secrets Manager.
_COOKIE_SECRET = "hardcoded-secret-changeme"
_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days
_cookie_signer = URLSafeTimedSerializer(
    secret_key=_COOKIE_SECRET,
    salt="user_session",
)


def _parse_user_session_cookie_value(app_instance: APIGatewayHttpResolver) -> str | None:
    """Extract raw user_session cookie value from event (cookies array or Cookie header)."""
    event = app_instance.current_event
    cookies = getattr(event, "cookies", None)
    if isinstance(cookies, list):
        for item in cookies:
            if not isinstance(item, str):
                continue
            if item.strip().startswith("user_session="):
                try:
                    raw = item.split("user_session=", 1)[1].split(";")[0].strip()
                    return raw if raw else None
                except Exception:  # pylint: disable=broad-except
                    pass
    headers = getattr(event, "headers", None) or {}
    cookie = (headers.get("Cookie") or headers.get("cookie") or "").strip()
    if "user_session=" not in cookie:
        return None
    try:
        raw = cookie.split("user_session=")[1].split(";")[0].strip()
        return raw if raw else None
    except Exception:  # pylint: disable=broad-except
        return None


def _get_email_from_cookie(app_instance: APIGatewayHttpResolver) -> str | None:
    """Verify and deserialize user_session cookie; return email only if valid and non-empty.
    Cookie value must be signed with itsdangerous. Empty or invalid cookie => not signed in.
    """
    raw = _parse_user_session_cookie_value(app_instance)
    if not raw:
        return None
    try:
        email = _cookie_signer.loads(raw, max_age=_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired, Exception):  # pylint: disable=broad-except
        logger.debug("user_session cookie invalid or expired")
        return None
    if not email or not isinstance(email, str) or "@" not in email:
        return None
    return email.strip()


def check_auth(app_instance: APIGatewayHttpResolver, next_middleware: Callable) -> Response:
    """Verify user_session cookie (set after OIDC login) and inject email into context."""
    email = _get_email_from_cookie(app_instance)
    if not email:
        logger.debug("check_auth: no user_session cookie, returning 401")
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
    logger.debug("conditional_api_auth path=%s requires_auth=%s", path, path.startswith("/api"))
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
        # Signed cookie so it cannot be tampered with; value must be present for "signed in"
        signed_value = _cookie_signer.dumps(email)
        session_cookie = Cookie(
            name="user_session",
            value=signed_value,
            path="/",
            http_only=True,
            same_site=SameSite.LAX_MODE,
            secure=True,
        )
        logger.info("auth_callback success email=%s redirect=/", email)
        return Response(
            status_code=302,
            headers={"Location": "/"},
            cookies=[session_cookie],
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
    email = _get_email_from_cookie(app)
    has_cookie = email is not None
    template_name = "camera.html" if has_cookie else "landing.html"
    logger.info("serve_index has_cookie=%s template=%s", has_cookie, template_name)
    try:
        if has_cookie:
            template = jinja_env.get_template(template_name)
            html = template.render(user_email=email)
        else:
            template_path = Path(__file__).parent / "templates" / template_name
            with template_path.open("r", encoding="utf-8") as f:
                html = f.read()
        return Response(
            status_code=200,
            content_type=content_types.TEXT_HTML,
            body=html,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to load template %s: %s", template_name, exc)
        return Response(status_code=404, body="Not Found")


@app.get("/auth/logout")
def auth_logout() -> Response:
    """Delete session cookie (Max-Age=0, Expires past) and redirect to landing page."""
    # Same name/path as set cookie; value empty + max_age=0 + expires past so browser removes it
    delete_cookie = Cookie(
        name="user_session",
        value="",
        path="/",
        max_age=0,
        expires=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    return Response(
        status_code=302,
        headers={"Location": "/"},
        cookies=[delete_cookie],
        body="",
    )

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

def get_dir_content(which, proxy: str):
    """Safely finds and reads static files from the /static folder."""
    logger.error("get_dir_context(%s,%s)",which,proxy)
    base_dir = os.path.dirname(__file__)

    # Securely join and resolve the path to prevent directory traversal
    path = os.path.abspath(os.path.join(base_dir, which, proxy))
    static_root = os.path.abspath(os.path.join(base_dir, which))

    if not path.startswith(static_root):
        return None, 403 # Forbidden (Traversal attempt)

    if not os.path.exists(path) or not os.path.isfile(path):
        return None, 404 # Not Found

    mtype, _ = mimetypes.guess_type(path)
    # Ensure common web types are correct
    if path.endswith('.js'):
        mtype = 'application/javascript'
    elif path.endswith('.css'):
        mtype = 'text/css'

    # Read as binary to let Powertools handle auto-Base64 encoding if needed
    with open(path, "rb") as f:
        return f.read(), mtype

@app.get("/static/.+")
def serve_static():
    """Serves CSS, JS, and Images from the static/ directory."""
    file_path = app.current_event.path.replace("/static/", "")

    logger.error("serve_static(%s)",file_path)
    content, status_or_type = get_dir_content("static",file_path)

    if status_or_type == 403:
        return Response(status_code=403, body="Forbidden", content_type="text/plain")
    if status_or_type == 404:
        return Response(status_code=404, body="File Not Found", content_type="text/plain")

    return Response(
        status_code=200,
        content_type=status_or_type,
        body=content # Powertools auto-encodes binary 'bytes' to Base64
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
    # Debug: log every invocation with event summary (and full event at DEBUG level)
    logger.debug("lambda_handler event: %s", json.dumps(event, default=str))
    _method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
    _path = event.get("requestContext", {}).get("http", {}).get("path") or event.get("rawPath") or event.get("path", "")
    print(f"[DEBUG] lambda_handler: source={event.get('source')} method={_method} path={_path} pathParams={event.get('pathParameters')}")
    logger.info("Request: method=%s path=%s", _method, _path)

    if event.get("source") == "aws.s3":
        detail = event.get("detail")
        if detail is None or not isinstance(detail, dict):
            logger.error("S3 event missing or invalid detail")
            return {"statusCode": 400, "body": "Missing detail"}
        logger.info("Processing S3 EventBridge trigger")
        return handle_s3_event(detail)

    if event.get("source") == "aws.events":
        return {"warmed": True}

    return app.resolve(event, context)
