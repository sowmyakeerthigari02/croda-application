"""Employer account passwords, encrypted and kept separate per applicant, site and login email.

Nothing here returns a password to the API. Only the browser worker reads one, to type it into
the exact site it was saved for.
"""
import secrets
import string
from urllib.parse import parse_qs, urlparse

from . import config, db

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"

_SYMBOLS = "!#%*-_+="
# Portals where different employers share a hostname and differ only by a URL parameter.
_SHARED_HOST_PARAMS = {"successfactors": "company"}


def site_key(url: str) -> str:
    """The exact host a password belongs to, so a lookalike domain never receives it."""
    parsed = urlparse(url if "//" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    for marker, param in _SHARED_HOST_PARAMS.items():
        if marker in host:
            tenant = parse_qs(parsed.query, keep_blank_values=False)
            for name, values in tenant.items():
                if name.lower() == param:
                    return f"{host}#{values[0].lower()}"
    return host


def usable_on(url: str) -> bool:
    """Passwords only ever go over HTTPS."""
    return urlparse(url).scheme == "https" or config.ALLOW_PRIVATE_URLS


def generate_password() -> str:
    """Unique per account: 16 characters fits common portal rules such as 8-18 with no spaces."""
    required = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
                secrets.choice(string.digits), secrets.choice(_SYMBOLS)]
    pool = string.ascii_letters + string.digits + _SYMBOLS
    chars = required + [secrets.choice(pool) for _ in range(12)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def save(applicant_id: str, site: str, login_email: str, password: str, status: str = STATUS_ACTIVE) -> str:
    credential_id = db.new_id()
    now = db.now()
    db.execute(
        "INSERT INTO credentials(id,applicant_id,site,login_email,secret,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(applicant_id,site,login_email) DO UPDATE SET "
        "secret=excluded.secret, status=excluded.status, updated_at=excluded.updated_at",
        credential_id, applicant_id, site, login_email.strip().lower(),
        config.VAULT.encrypt(password.encode()), status, now, now,
    )
    return credential_id


def find(applicant_id: str, site: str, preferred_email: str | None) -> dict | None:
    """The account to use here: the profile email's, or the only one. Otherwise nothing is guessed."""
    rows = db.query("SELECT id, login_email, secret, status FROM credentials WHERE applicant_id=? AND site=?",
                    applicant_id, site)
    wanted = (preferred_email or "").strip().lower()
    match = next((r for r in rows if r["login_email"] == wanted), None)
    if match is None and len(rows) == 1:
        match = rows[0]
    if match is None:
        return None
    return {"id": match["id"], "login_email": match["login_email"], "status": match["status"],
            "password": config.VAULT.decrypt(bytes(match["secret"])).decode()}


def has_any(applicant_id: str, site: str) -> bool:
    return db.one("SELECT id FROM credentials WHERE applicant_id=? AND site=? LIMIT 1", applicant_id, site) is not None


def list_for(applicant_id: str) -> list[dict]:
    return db.query("SELECT id, site, login_email, status, updated_at FROM credentials WHERE applicant_id=? "
                    "ORDER BY site, login_email", applicant_id)


def mark(credential_id: str, status: str) -> None:
    db.execute("UPDATE credentials SET status=?, updated_at=? WHERE id=?", status, db.now(), credential_id)


def auto_accounts_since(applicant_id: str) -> str | None:
    """When the applicant allowed accounts to be created without asking. Off until they turn it on."""
    row = db.one("SELECT granted_at FROM account_consent WHERE applicant_id=?", applicant_id)
    return row["granted_at"] if row else None


def set_auto_accounts(applicant_id: str, enabled: bool) -> None:
    db.execute("DELETE FROM account_consent WHERE applicant_id=?", applicant_id)
    if enabled:
        db.execute("INSERT INTO account_consent(applicant_id,granted_at) VALUES(?,?)", applicant_id, db.now())


def delete(applicant_id: str, credential_id: str) -> None:
    db.execute("DELETE FROM credentials WHERE id=? AND applicant_id=?", credential_id, applicant_id)
