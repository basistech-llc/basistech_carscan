import functools
import requests
import sys
import json
import base64
import urllib
import urllib.parse
from pathlib import Path
import argparse

#from secrets import API_KEY, CLIENT_PASSWORD_ID, CLIENT_PASSWORD_SECRET, ADMIN_ID, USER_PASSWORD

import logging
from typing import Final

SECRET_PATH = Path(__file__).parent.parent.parent / "secrets_brivo.json"


# basistech configuration

DOMAIN = 'auth.brivo.com'
BASE_API = 'https://api.brivo.com/v1/api'
PLATE_FIELD = "Auto Registration - Plate Number"
PLATE_CUSTOM_FIELD_ID = 629120  # from your HAR (Auto Registration - Plate Number)
_CF_ID_CACHE = {}

OPERATOR_EQ="eq"
OPERATOR_CONTAINS="contains"
OPERATOR_STARTS="startswith"

LOGGER: Final = logging.getLogger(__name__)

@functools.lru_cache(maxsize=1)
def get_secrets():
    if SECRET_PATH.exists:
        with SECRET_PATH.open("r") as f:
            return json.load(f)

def secret(v):
    return get_secrets()[v]

@functools.lru_cache(maxsize=1)
def get_token():
    CLIENT_PASSWORD_ID = secret('CLIENT_PASSWORD_ID')
    CLIENT_PASSWORD_SECRET = secret('CLIENT_PASSWORD_SECRET')
    API_KEY = secret('API_KEY')
    ADMIN_ID = secret('ADMIN_ID')
    USER_PASSWORD = secret('USER_PASSWORD')

    url = f'https://{DOMAIN}/oauth/token'
    credentials = base64.b64encode(f"{CLIENT_PASSWORD_ID}:{CLIENT_PASSWORD_SECRET}".encode()).decode()
    headers = {
        'Authorization': f'Basic {credentials}',
        'api-key': API_KEY,
        'Content-type': 'application/x-www-form-urlencoded'
    }
    params = {'grant_type': 'password', 'username': ADMIN_ID, 'password': USER_PASSWORD}

    resp = requests.post(url, headers=headers, data=urllib.parse.urlencode(params))
    resp.raise_for_status()
    return resp.json()

def authenticated_request(method, endpoint, params=None):
    API_KEY = secret('API_KEY')
    ACCESS_TOKEN = get_token()['access_token']

    url = f"{BASE_API}{endpoint}"
    headers = {
        'Authorization': f"bearer {ACCESS_TOKEN}",
        'api-key': API_KEY
    }

    try:
        if method.lower() == 'get':
            print(url,params)
            r = requests.get(url, headers=headers, params=params)
        else:
            raise ValueError(method)

        if r.status_code == 401:
            print("--> [Auth] Token expired. Refreshing...", file=sys.stderr)
            headers['Authorization'] = f"bearer {ACCESS_TOKEN}"
            print(url,params)
            r = requests.get(url, headers=headers, params=params)

        r.raise_for_status()
        return r

    except Exception as e:
        print(f"!! API Error on {url}: {e}", file=sys.stderr)
        return None


def get_user_detail(user_id):
    r = authenticated_request('GET',f'/users/{user_id}',
                              params = {"expand": "customFields,emails,phoneNumbers,credentials"})
    r.raise_for_status()
    return r.json()

def get_all_users(pageSize=100,expand=False):
    """Generator to get all users"""
    offset = 0
    while True:
        print("offset:",offset)
        r = authenticated_request('GET','/users', params = {
            "offset": offset,
            "pageSize": pageSize, })

        data = r.json()
        users = data.get("data", [])
        print("len(users)=",len(users))
        if not users:
            return

        for u in users:
            if expand:
                u = get_user_detail(u)
            yield u

        offset += pageSize

def find_plate(user):
    for cf in user.get("customFields", []):
        if cf.get("name") == PLATE_FIELD:
            return cf.get("value")
    return None


def resolve_custom_field_id_by_name_via_public_api(field_name):
    """
    Resolve custom-field ID by hitting /custom-fields on api.brivo.com.
    This runs once and caches the result.
    """
    key = field_name.strip().lower()
    if key in _CF_ID_CACHE:
        return _CF_ID_CACHE[key]

    # IMPORTANT: this endpoint name is hyphenated, and it returns fieldName (not name)
    r = authenticated_request("GET", "/custom-fields", params={"offset": 0, "pageSize": 1000})
    if r is None:
        raise RuntimeError("GET /custom-fields failed (None).")

    payload = r.json()
    items = payload.get("data", [])
    for item in items:
        fn = (item.get("fieldName") or "").strip().lower()
        if fn == key:
            cfid = item.get("id")
            if cfid is None:
                break
            _CF_ID_CACHE[key] = int(cfid)
            return int(cfid)

    raise ValueError(f"Could not find custom field id for {field_name!r} via /custom-fields")


def search_users_by_plate_api( plate, *,
                               plate_field_name=PLATE_FIELD,
                               custom_field_id=PLATE_CUSTOM_FIELD_ID,
                               page_size=100,
                               expand="customFields,emails,phoneNumbers,credentials",
                               operator="eq" ):
    """
    Server-side search: /users?filter=cf_<id>__eq:<plate>
    Brivo documents this cf_<id>__eq syntax. :contentReference[oaicite:2]{index=2}
    """
    plate = (plate or "").strip()
    if not plate:
        return []

    # If you hardcode PLATE_CUSTOM_FIELD_ID, no lookup needed.
    # If you want it to self-heal when IDs change, set custom_field_id=None.
    if custom_field_id is None:
        custom_field_id = resolve_custom_field_id_by_name_via_public_api(plate_field_name)

    filter_expr = f"cf_{int(custom_field_id)}__{operator}:{plate}"

    r = authenticated_request("GET", "/users", params={
        "offset": 0,
        "pageSize": page_size,
        "expand": expand,
        "filter": filter_expr,
    })
    if r is None:
        return []

    payload = r.json()
    return payload.get("data", [])

def brivo_lookup(plate: str, state: str) -> str:
    """
    Stub Brivo lookup.

    Args:
        plate: License plate text (already upper‑cased by caller).
        state: Two‑letter state code.

    Returns:
        A display name string for the matched user, or ``\"Unknown\"``.
        For now this is a pure stub and always returns ``\"Unknown\"``.
    """
    LOGGER.debug("Brivo lookup stub called for plate=%s state=%s", plate, state)
    return "Unknown"


def main():
    parser = argparse.ArgumentParser(description='Upload an image',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--dump",  help='dump all license plates and names', action='store_true')
    parser.add_argument('--plate', help='search for a plate')
    parser.add_argument('--name',  help='search for a name')
    parser.add_argument('--op', help='operator', default='eq')
    args = parser.parse_args()

    if args.dump:
        for u in get_all_users():
            print(u['id'], u['firstName'], u['lastName'], find_plate(u))
    elif args.plate:
        users = search_users_by_plate_api(args.plate,operator=args.op)
        for u in users:
            print(u["id"], u["firstName"], u["lastName"], find_plate(u))
    elif args.name:
        hits = search_users_by_plate_api(args.name,operator=OPERATOR_CONTAINS)   # server-side
        for u in hits:
            print(u["id"], u.get("firstName"), u.get("lastName"), find_plate(u))
        return

if __name__=="__main__":
    main()
