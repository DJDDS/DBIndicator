"""
Handles the one-touch-per-day Kite Connect login.

Zerodha issues a fresh access token every trading day (this is a fixed
security policy on their end, not something any app can configure
around) - so each morning you'll click a login link, sign in with your
Zerodha password + 2FA, and this module exchanges the resulting
request_token for the day's access_token. That token is cached to a
local file and reused for every request until it expires (early the
next morning), so this is the ONLY manual step in the whole app.
"""
import json
import os
from datetime import date

from kiteconnect import KiteConnect

from . import config


def _load_cache():
    if not os.path.exists(config.TOKEN_CACHE_FILE):
        return None
    try:
        with open(config.TOKEN_CACHE_FILE) as f:
            data = json.load(f)
        if data.get("date") == date.today().isoformat():
            return data.get("access_token")
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _save_cache(access_token):
    with open(config.TOKEN_CACHE_FILE, "w") as f:
        json.dump({"date": date.today().isoformat(), "access_token": access_token}, f)


def get_login_url():
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    return kite.login_url()


def exchange_request_token(request_token):
    """Called from the /kite/callback route once Zerodha redirects back
    with a request_token. Returns the access_token and caches it for
    the rest of the day."""
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    session_data = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    access_token = session_data["access_token"]
    _save_cache(access_token)
    return access_token


def get_kite_client():
    """Returns an authenticated KiteConnect client if today's token is
    already cached, or None if a login is still needed today."""
    access_token = _load_cache()
    if not access_token:
        return None
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    kite.set_access_token(access_token)
    return kite


def is_logged_in_today():
    return _load_cache() is not None
