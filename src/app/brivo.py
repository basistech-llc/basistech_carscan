import requests
import sys
import json
import base64
import urllib
import urllib.parse
from pathlib import Path

#from secrets import API_KEY, CLIENT_PASSWORD_ID, CLIENT_PASSWORD_SECRET, ADMIN_ID, USER_PASSWORD

import logging
from typing import Final

LOGGER: Final = logging.getLogger(__name__)

# basistech configuration

DOMAIN = 'auth.brivo.com'
BASE_API = 'https://api.brivo.com/v1/api'
PLATE_FIELD = "Auto Registration - Plate Number"
PLATE_CUSTOM_FIELD_ID = 629120  # from your HAR (Auto Registration - Plate Number)
_CF_ID_CACHE = {}

SECRET_PATH = Path(__file__).parent.parent.parent / "secrets_brivo.json"

def get_secrets():
    if SECRET_PATH.exists:
        with SECRET_PATH.open("r") as f:
            return json.load(f)


def get_token():
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

CURRENT_TOKEN = None
def authenticated_request(method, endpoint, params=None):
    global CURRENT_TOKEN
    if CURRENT_TOKEN is None:
        CURRENT_TOKEN = get_token()

    url = f"{BASE_API}{endpoint}"
    headers = {
        'Authorization': f"bearer {CURRENT_TOKEN['access_token']}",
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
            CURRENT_TOKEN = get_token()
            headers['Authorization'] = f"bearer {CURRENT_TOKEN['access_token']}"
            print(url,params)
            r = requests.get(url, headers=headers, params=params)

        r.raise_for_status()
        return r

    except Exception as e:
        print(f"!! API Error on {url}: {e}", file=sys.stderr)
        return None


def list_users(page_size=100):
    page = 0
    while True:
        r = authenticated_request('GET','/users', params = {
            "page": page,
            "pageSize": page_size, })

        data = r.json()
        users = data.get("data", [])
        if not users:
            return

        for u in users:
            yield u

        page += 1

def get_user_detail(user_id):
    r = authenticated_request('GET',f'/users/{user_id}',    params = {"expand": "customFields,emails,phoneNumbers,credentials"})
    r.raise_for_status()
    return r.json()

def get_all_users_full():
    out = []
    for u in list_users():
        uid = u["id"]
        full = get_user_detail(uid)
        print(full)
        out.append(full)
    return out


def find_plate(user):
    for cf in user.get("customFields", []):
        if cf.get("name") == PLATE_FIELD:
            return cf.get("value")
    return None


def search_by_name(users, text):
    text = text.lower()
    hits = []
    for u in users:
        fn = (u.get("firstName") or "").lower()
        ln = (u.get("lastName") or "").lower()
        if text in fn or text in ln:
            hits.append(u)
    return hits



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


def search_users_by_plate_api(
    plate,
    *,
    plate_field_name=PLATE_FIELD,
    custom_field_id=PLATE_CUSTOM_FIELD_ID,
    page_size=100,
    expand="customFields,emails,phoneNumbers,credentials",
    operator="eq",
):
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


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  dump")
        print("  search-name \"Simson\"")
        print("  search-plate \"EV F895\"")
        return

    cmd = sys.argv[1]


    if cmd == "dump":
        users = get_all_users_full()
        with open("brivo_users.json", "w") as f:
            json.dump(users, f, indent=2)
        print(f"Wrote {len(users)} users")

    elif cmd == "search-name":
        q = sys.argv[2]
        users = get_all_users_full()
        hits = search_by_name(users, q)
        for u in hits:
            print(u["id"], u["firstName"], u["lastName"], find_plate(u))

    elif cmd == "search-plate":
        q = sys.argv[2]
        hits = search_users_by_plate_api(q)   # server-side
        print(hits)
        for u in hits:
            print(u["id"], u.get("firstName"), u.get("lastName"), find_plate(u))
        return

    else:
        print("Unknown command")




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

if __name__=="__main__":
    print(get_secrets())
