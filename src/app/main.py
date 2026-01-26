import os
import mimetypes
import logging

# https://docs.aws.amazon.com/powertools/python/latest/tutorial/
# https://docs.aws.amazon.com/powertools/python/latest/core/event_handler/api_gateway/#using-regex-patterns

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

logger = Logger(service="APP") # Automatically picks up LOG_LEVEL from env
logger.setLevel(logging.INFO)
app = APIGatewayHttpResolver(enable_validation=True)
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
jinja_env = Environment(loader=FileSystemLoader(template_dir))

# --- 3. Internal Helper Functions ---

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

def render_dynamic_template(template_name: str) -> Response:
    """Helper to find a template, inject query params, and return a Response."""
    logger.error("render_dynamic_template(%s)",template_name)

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

@app.not_found
def handle_not_found_route(rt) -> str:
    # Log the event details, return a custom message, or raise a different error
    return Response(status_code=404,
                    body="Sorry, we couldn't find that page/resource!",
                    content_type=content_types.TEXT_PLAIN)

@app.get("/")
def get_index():
    """Explicitly handle the root path."""
    return render_dynamic_template("index.html")

@app.get("/hello")
def hello() -> dict:
    return {"message": "Hello world!"}

@app.get("/hello/<name>")
def hello_name(name):
    logger.info(f"Request from {name} received")
    return {"message": f"hello {name}!"}

@app.post("/contact")
def handle_contact_form():
    """Handles a POST request from a contact form."""
    # 1. Get the form data.
    # If the form is a standard HTML form, it's url-encoded.
    # Powertools makes the parsed body available via .json_body if it's JSON,
    # or you can use .decoded_body for raw text.
    form_data = app.current_event.json_body if app.current_event.json_body else app.current_event.body

    logger.info(f"Received contact form submission: {form_data}")

    # 2. Logic (e.g., send an email via SES, save to DynamoDB, etc.)
    # For now, we'll just render a 'thank you' message.
    return Response(
        status_code=200,
        content_type=content_types.TEXT_HTML,
        body=f"<h1>Thank you!</h1><p>We received your message: {form_data}</p><a href='/'>Back Home</a>"
    )

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

@app.get("/assets/.+")
def serve_assets():
    """Serves CSS, JS, and Images from the assets/ directory."""
    file_path = app.current_event.path.replace("/assets/", "")
    logger.error("serve_assets(%s)",file_path)
    content, status_or_type = get_dir_content("assets",file_path)

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
    logger.info("catch_all_templates(%s)",proxy)
    return render_dynamic_template(proxy)

# --- 5. Main Lambda Handler ---
def lambda_handler(event, context):
    # Handle EventBridge/CloudWatch Heartbeats (Warm-up)
    logger.debug("event=%s context=%s",event,context)
    if event.get("source") == "aws.events":
        logger.info("aws.events event=%s",event)
        return {"warmed": True}

    # app.resolve handles the routing and converts our Response
    # objects into the dictionaries Lambda expects.
    return app.resolve(event, context)
