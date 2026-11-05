"""Runtime settings, all read from the environment (see .env.example)."""
import os
from pathlib import Path

from cryptography.fernet import Fernet

APP_ENV = os.environ.get("APP_ENV", "dev")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "applypilot.db"

# Supabase holds the data in production. Without these, dev uses SQLite and a local folder.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "applypilot")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
MAX_LIVE_BROWSERS = int(os.environ.get("MAX_LIVE_BROWSERS", "2"))
PAUSE_TIMEOUT_SECONDS = int(os.environ.get("PAUSE_TIMEOUT_SECONDS", "1800"))
CODE_WAIT_SECONDS = int(os.environ.get("CODE_WAIT_SECONDS", "90"))
CODE_POLL_SECONDS = int(os.environ.get("CODE_POLL_SECONDS", "5"))
ALLOW_PRIVATE_URLS = os.environ.get("ALLOW_PRIVATE_URLS", "0") == "1"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
VIEWPORT = {"width": 1280, "height": 800}

if APP_ENV != "dev" and not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD must be set outside dev: this app holds resumes and personal data.")
if APP_ENV != "dev" and not (DATABASE_URL and SUPABASE_URL and SUPABASE_SERVICE_KEY):
    raise RuntimeError("DATABASE_URL, SUPABASE_URL and SUPABASE_SERVICE_KEY must be set outside dev.")
if bool(SUPABASE_URL) != bool(SUPABASE_SERVICE_KEY):
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set together.")


def _key(name: str, purpose: str) -> bytes:
    key = os.environ.get(name)
    if key:
        return key.encode()
    if APP_ENV != "dev":
        raise RuntimeError(f"{name} must be set outside dev: it encrypts {purpose}.")
    path = DATA_DIR / f".dev_{name.lower()}"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
    return path.read_bytes()


FERNET = Fernet(_key("SESSION_KEY", "saved browser sign-ins"))
VAULT = Fernet(_key("VAULT_KEY", "saved employer account passwords"))
