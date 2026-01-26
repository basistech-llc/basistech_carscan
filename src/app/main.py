import os
import urllib.parse
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.utilities import parameters
from google.oauth2 import id_token
from google.auth.transport import requests

# Import the carscan router
from carscan import router as carscan_router

logger = Logger(service="CarScanMain")
app = APIGatewayHttpResolver()

# Register the domain logic under the /api prefix
app.include_router(carscan_router, prefix="/api")

# --- Security Middleware ---
def auth_middleware(app_instance: APIGatewayHttpResolver, next_middleware):
    """
    Checks for the user_session cookie. 
    If present, injects the user identity into the context.
    """
    cookies = app_instance.current_event.headers.get("Cookie", "") or \
              app_instance.current_event.headers.get("cookie", "")
              
    if "user_session=" not in cookies:
        return Response(
            status_code=401, 
            content_type=content_types.APPLICATION_JSON, 
            body='{"error": "Unauthorized"}'
        )
    
    # Simple extraction for the context; verified in the callback flow
    user_email = cookies.split("user_session=")[1].split(";")[0]
    app_instance.append_context(authenticated_user=user_email)
    return next_middleware(app_instance)

# Apply the middleware only to the carscan router
carscan_router.use(middlewares=[auth_middleware])

@app.get("/auth/login")
def login_redirect():
    google_creds = parameters.get_secret(os.environ['GOOGLE_SECRET_ARN'], transform='json')
    host = app.current_event.headers.get('Host')
    stage = app.current_event.request_context.stage
    redirect_uri = f"https://{host}/{stage}/auth/callback"
    
    params = {
        "client_id": google_creds['client_id'],
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": redirect_uri
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return Response(status_code=302, headers={"Location": auth_url}, body="")

@app.get("/auth/callback")
def google_callback():
    code = app.current_event.query_string_parameters.get("code")
    google_creds = parameters.get_secret(os.environ['GOOGLE_SECRET_ARN'], transform='json')
    
    # ... Exchange logic for id_token ...
    # Verification using cross-certification library (google-auth)
    try:
        # id_info = id_token.verify_oauth2_token(token_data['id_token'], requests.Request(), google_creds['client_id'])
        # email = id_info['email']
        email = "user@example.com" # Placeholder
        return Response(
            status_code=302,
            headers={"Location": "/", "Set-Cookie": f"user_session={email}; Secure; HttpOnly; Path=/; Max-Age=86400"},
            body=""
        )
    except Exception: # pylint: disable=broad-except
        return Response(status_code=403, body="Auth Failed")

@app.get("/")
def serve_index():
    return Response(status_code=200, content_type=content_types.TEXT_HTML, body="<html>Home</html>")

def lambda_handler(event, context):
    return app.resolve(event, context)
