import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

XERO_CLIENT_ID = os.environ["XERO_CLIENT_ID"]
XERO_CLIENT_SECRET = os.environ["XERO_CLIENT_SECRET"]
XERO_REDIRECT_URI = os.getenv(
    "XERO_REDIRECT_URI",
    "http://localhost:8000/callback",
)

GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]
BQ_DATASET = os.getenv("BQ_DATASET", "custom_xero")

XERO_DAILY_LIMIT = int(os.getenv("XERO_DAILY_LIMIT", "1000"))
XERO_MINUTE_LIMIT = int(os.getenv("XERO_MINUTE_LIMIT", "60"))

STATE_DIR = Path(os.getenv("STATE_DIR", ".state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)

TOKEN_FILE = STATE_DIR / "token.json"
TENANT_FILE = STATE_DIR / "tenant.json"
RATE_FILE = STATE_DIR / "rate_limit.json"
WATERMARK_FILE = STATE_DIR / "watermarks.json"

XERO_AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
XERO_API_BASE = "https://api.xero.com/api.xro/2.0"

XERO_SCOPES = (
    "offline_access "
    "accounting.contacts "
    "accounting.invoices"
)