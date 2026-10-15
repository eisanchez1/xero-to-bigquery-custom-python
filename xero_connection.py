import base64
import json
import secrets
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from config import (
    TOKEN_FILE,
    TENANT_FILE,
    RATE_FILE,
    XERO_CLIENT_ID,
    XERO_CLIENT_SECRET,
    XERO_REDIRECT_URI,
    XERO_AUTHORIZE_URL,
    XERO_TOKEN_URL,
    XERO_CONNECTIONS_URL,
    XERO_API_BASE,
    XERO_SCOPES,
    XERO_DAILY_LIMIT,
    XERO_MINUTE_LIMIT,
)


class XeroConnection:
    def __init__(self):
        self.session = requests.Session()
        self.token = self._read_json(TOKEN_FILE, {})
        self.tenant = self._read_json(TENANT_FILE, {})
        self.rate = self._read_json(
            RATE_FILE,
            {"date": "", "daily_calls": 0, "minute_calls": []},
        )

    @staticmethod
    def _read_json(path, default):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return default

    @staticmethod
    def _write_json(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _save_token(self, token):
        token["expires_at"] = time.time() + token["expires_in"]
        self.token = token
        self._write_json(TOKEN_FILE, token)

    def authorize(self):
        """Run once to authorize the app and select a Xero organization."""
        state = secrets.token_urlsafe(32)
        callback_result = {}

        parsed = urlparse(XERO_REDIRECT_URI)
        if parsed.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError(
                "The initial authorization flow requires a localhost "
                "redirect URI."
            )

        port = parsed.port or 80
        callback_path = parsed.path or "/"

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(handler_self):
                request_url = urlparse(handler_self.path)
                params = parse_qs(request_url.query)

                if request_url.path != callback_path:
                    handler_self.send_error(404)
                    return

                if params.get("state", [""])[0] != state:
                    callback_result["error"] = "OAuth state mismatch."
                elif "error" in params:
                    callback_result["error"] = params["error"][0]
                else:
                    callback_result["code"] = params.get(
                        "code", [""]
                    )[0]

                body = (
                    b"Authorization received. You can close this window."
                    if "code" in callback_result
                    else b"Authorization failed. Check the terminal."
                )
                handler_self.send_response(200)
                handler_self.send_header(
                    "Content-Type", "text/plain; charset=utf-8"
                )
                handler_self.end_headers()
                handler_self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = HTTPServer((parsed.hostname, port), CallbackHandler)

        params = {
            "response_type": "code",
            "client_id": XERO_CLIENT_ID,
            "redirect_uri": XERO_REDIRECT_URI,
            "scope": XERO_SCOPES,
            "state": state,
        }
        auth_url = f"{XERO_AUTHORIZE_URL}?{urlencode(params)}"

        print("Open this URL in your browser and authorize Xero:")
        print(auth_url)
        print("\nWaiting for the Xero callback...")

        try:
            while not callback_result:
                server.handle_request()
        finally:
            server.server_close()

        if "error" in callback_result:
            raise RuntimeError(callback_result["error"])

        response = requests.post(
            XERO_TOKEN_URL,
            auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
            data={
                "grant_type": "authorization_code",
                "code": callback_result["code"],
                "redirect_uri": XERO_REDIRECT_URI,
            },
            timeout=30,
        )
        response.raise_for_status()
        self._save_token(response.json())

        connections = self._authenticated_get_connections()
        if not connections:
            raise RuntimeError(
                "No Xero organization is connected to this app."
            )

        print("\nAvailable Xero organizations:")
        for index, connection in enumerate(connections, start=1):
            print(f"{index}. {connection.get('tenantName', 'Unnamed')}")

        while True:
            choice = input(
                f"Select the organization to use (1-{len(connections)}): "
            )
            try:
                selected = connections[int(choice) - 1]
                break
            except (ValueError, IndexError):
                print("Enter a valid number.")

        self.tenant = {
            "tenant_id": selected["tenantId"],
            "tenant_name": selected.get("tenantName", ""),
        }
        self._write_json(TENANT_FILE, self.tenant)
        print(f"Saved organization: {self.tenant['tenant_name']}")

    def _refresh_token(self):
        if not self.token.get("refresh_token"):
            raise RuntimeError(
                "No refresh token found. Run: python main.py authorize"
            )

        response = requests.post(
            XERO_TOKEN_URL,
            auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.token["refresh_token"],
            },
            timeout=30,
        )
        response.raise_for_status()

        # Xero rotates refresh tokens. Save the new token immediately.
        self._save_token(response.json())

    def access_token(self):
        if not self.token.get("refresh_token"):
            raise RuntimeError(
                "Xero is not authorized. Run: python main.py authorize"
            )

        if time.time() >= self.token.get("expires_at", 0) - 60:
            self._refresh_token()

        return self.token["access_token"]

    def _authenticated_get_connections(self):
        response = requests.get(
            XERO_CONNECTIONS_URL,
            headers={"Authorization": f"Bearer {self.access_token()}"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def _reserve_api_call(self):
        """Enforce the daily and rolling 60-second limits locally."""
        while True:
            now = time.time()
            today = datetime.now(timezone.utc).date().isoformat()

            if self.rate.get("date") != today:
                self.rate = {
                    "date": today,
                    "daily_calls": 0,
                    "minute_calls": [],
                }

            recent = [
                timestamp
                for timestamp in self.rate.get("minute_calls", [])
                if now - timestamp < 60
            ]
            self.rate["minute_calls"] = recent

            if self.rate["daily_calls"] >= XERO_DAILY_LIMIT:
                raise RuntimeError(
                    f"Local daily limit of {XERO_DAILY_LIMIT} "
                    "Xero API calls reached. Try again after UTC midnight."
                )

            if len(recent) >= XERO_MINUTE_LIMIT:
                wait_seconds = max(1, 60 - (now - recent[0]))
                print(f"Minute limit reached; waiting {wait_seconds:.1f}s")
                time.sleep(wait_seconds)
                continue

            self.rate["daily_calls"] += 1
            self.rate["minute_calls"].append(now)
            self._write_json(RATE_FILE, self.rate)
            return

    def get_accounting_page(self, endpoint, params, modified_since=None):
        """Get one page and retry HTTP 429 responses safely."""
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "xero-tenant-id": self.tenant["tenant_id"],
            "Accept": "application/json",
        }

        if modified_since:
            headers["If-Modified-Since"] = format_datetime(
                modified_since.astimezone(timezone.utc),
                usegmt=True,
            )

        url = f"{XERO_API_BASE}/{endpoint}"

        while True:
            self._reserve_api_call()

            response = self.session.get(
                url,
                headers=headers,
                params=params,
                timeout=60,
            )

            if response.status_code == 429:
                delay = int(response.headers.get("Retry-After", "5"))
                print(f"Xero returned HTTP 429; waiting {delay}s")
                time.sleep(max(1, delay))
                continue

            response.raise_for_status()
            return response.json()