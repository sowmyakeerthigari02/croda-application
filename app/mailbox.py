"""Reads emailed verification codes and links from the applicant's connected mailbox.

The mailbox is opened read-only, and only mail from the site that is asking is downloaded. That
sender check is what stops a lookalike site getting a code meant for a bank or for Google. Email
text is only ever searched for a code: it never reaches the AI, the logs or the database.
"""
import email
import email.policy
import html
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import urlparse

from . import config, db

log = logging.getLogger("applypilot.mailbox")

STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"

IMAP_TIMEOUT_SECONDS = 20
MAX_MESSAGES_PER_SENDER = 10
# Personal Outlook and Hotmail refuse app passwords, so they cannot be offered here.
_PROVIDERS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
}
_TWO_PART_SUFFIXES = {"co.uk", "ac.uk", "org.uk", "com.au", "co.in", "co.nz", "co.za", "co.jp", "com.br", "com.sg", "com.mx"}
# Job portals whose email comes from a different domain than their pages.
_PORTAL_SENDERS = {
    "myworkdayjobs.com": {"myworkday.com", "workday.com"},
    "myworkdaysite.com": {"myworkday.com", "workday.com"},
    "greenhouse.io": {"greenhouse-mail.io"},
}

_TOKEN = r"\b((?=[A-Z]*\d)[A-Z0-9]{4,8})\b"
_CODE_PATTERNS = (
    re.compile(r"(?i:\b(?:verification|security|confirmation|login|sign[- ]in|access|authentication|one[- ]time|your|the|this)"
               r"\s+(?:pass)?code)\D{0,40}?" + _TOKEN),
    re.compile(r"(?i:\b(?:otp|passcode|one[- ]time password))\D{0,40}?" + _TOKEN),
    re.compile(_TOKEN + r"(?i:\s+is\s+your\b)"),
)
_ANCHOR = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
_BARE_URL = re.compile(r"""https?://[^\s<>"')\]]+""", re.I)
_VERIFY_WORDS = re.compile(r"verif|confirm|activat|validat", re.I)
_NOT_VERIFY = re.compile(r"unsubscribe|opt-?out|privacy|terms|preferences", re.I)


class MailboxError(Exception):
    """The mailbox refused the connection. The message is safe to show the person."""


def host_for(address: str) -> str | None:
    return _PROVIDERS.get(address.rpartition("@")[2].strip().lower())


def _open(address: str, password: str) -> imaplib.IMAP4_SSL:
    host = host_for(address)
    if not host:
        raise MailboxError("That email provider is not supported yet. Use Gmail, Yahoo or iCloud.")
    try:
        connection = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT_SECONDS)
        connection.login(address, password)
        connection.select("INBOX", readonly=True)
    except (imaplib.IMAP4.error, OSError) as exc:
        log.warning("Mailbox connection failed: %s", type(exc).__name__)
        raise MailboxError("The email provider refused that address and app password.") from exc
    return connection


def _close(connection: imaplib.IMAP4_SSL) -> None:
    try:
        connection.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


def connect(applicant_id: str, address: str, password: str) -> None:
    """Checks the login works before saving it. App passwords are often shown with spaces in them."""
    address = address.strip().lower()
    password = re.sub(r"\s", "", password)
    _close(_open(address, password))
    now = db.now()
    db.execute(
        "INSERT INTO mailboxes(applicant_id,address,secret,status,created_at,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(applicant_id) DO UPDATE SET address=excluded.address, secret=excluded.secret, "
        "status=excluded.status, updated_at=excluded.updated_at",
        applicant_id, address, config.VAULT.encrypt(password.encode()), STATUS_ACTIVE, now, now,
    )


def get(applicant_id: str) -> dict | None:
    return db.one("SELECT address, status, updated_at FROM mailboxes WHERE applicant_id=?", applicant_id)


def disconnect(applicant_id: str) -> None:
    db.execute("DELETE FROM mailboxes WHERE applicant_id=?", applicant_id)


def registrable_domain(host: str) -> str:
    host = host.lower()
    parts = host.split(".")
    if len(parts) < 3 or parts[-1].isdigit():
        return host
    keep = 3 if ".".join(parts[-2:]) in _TWO_PART_SUFFIXES else 2
    return ".".join(parts[-keep:])


def sender_domains(url: str) -> set[str]:
    """The domains allowed to send a code for this page."""
    base = registrable_domain(urlparse(url).hostname or "")
    return {base} | _PORTAL_SENDERS.get(base, set())


def sent_by(from_header: str, domains: set[str]) -> bool:
    sender = parseaddr(from_header)[1].rpartition("@")[2].lower()
    return any(sender == d or sender.endswith(f".{d}") for d in domains)


def extract_code(subject: str, body: str) -> str | None:
    """The one code in this email. Two different candidates means asking the person, not guessing."""
    text = html.unescape(re.sub(r"<(style|script)\b.*?</\1>|<[^>]+>", " ", body, flags=re.I | re.S))
    found = set()
    for source in (subject, text):
        source = re.sub(r"\s+", " ", source)
        for pattern in _CODE_PATTERNS:
            found.update(pattern.findall(source))
    return found.pop() if len(found) == 1 else None


def extract_link(body: str, domains: set[str]) -> str | None:
    """The one account verification link in this email that stays on the site's own domains.

    Tracking redirects, other hosts and unsubscribe links never count, and two different
    candidates means asking the person, not guessing.
    """
    candidates = [(html.unescape(href), re.sub(r"<[^>]+>", " ", text)) for href, text in _ANCHOR.findall(body)]
    plain = re.sub(r"<[^>]+>", " ", body)
    candidates += [(html.unescape(link).rstrip(".,;"), "") for link in _BARE_URL.findall(plain)]
    found = set()
    for link, text in candidates:
        parts = urlparse(link)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        if registrable_domain(parts.hostname) not in domains:
            continue
        words = f"{parts.path} {parts.query} {text}"
        if _VERIFY_WORDS.search(words) and not _NOT_VERIFY.search(words):
            found.add(link)
    return found.pop() if len(found) == 1 else None


def _body(message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    try:
        return part.get_content() if part else ""
    except (LookupError, ValueError):
        return ""


def fetch_code(applicant_id: str, url: str, since: datetime, used: set[str]) -> str | None:
    """The newest unused code this site emailed after `since`, or None if none has arrived yet."""
    for message in _site_messages(applicant_id, url, since):
        code = extract_code(str(message["subject"] or ""), _body(message))
        if code and code not in used:
            return code
    return None


def fetch_link(applicant_id: str, url: str, since: datetime, used: set[str]) -> str | None:
    """The newest unused verification link this site emailed after `since`, or None if none has arrived yet."""
    domains = sender_domains(url)
    for message in _site_messages(applicant_id, url, since):
        link = extract_link(_body(message), domains)
        if link and link not in used:
            return link
    return None


def _site_messages(applicant_id: str, url: str, since: datetime) -> list:
    """Mail from this site sent after `since`, newest first."""
    row = db.one("SELECT address, secret FROM mailboxes WHERE applicant_id=?", applicant_id)
    if not row:
        raise MailboxError("No email is connected.")
    domains = sender_domains(url)
    try:
        connection = _open(row["address"], config.VAULT.decrypt(bytes(row["secret"])).decode())
    except MailboxError:
        db.execute("UPDATE mailboxes SET status=?, updated_at=? WHERE applicant_id=?", STATUS_FAILED, db.now(), applicant_id)
        raise
    try:
        # IMAP dates ignore time zones, so search from a day earlier and compare exact times below.
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        found = []
        for domain in domains:
            _, data = connection.search(None, f'(SINCE "{day}" FROM "{domain}")')
            for number in data[0].split()[-MAX_MESSAGES_PER_SENDER:]:
                # PEEK leaves the message unread in the person's inbox.
                _, parts = connection.fetch(number, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    found.append(email.message_from_bytes(raw, policy=email.policy.default))
    except (imaplib.IMAP4.error, OSError) as exc:
        log.warning("Mailbox read failed: %s", type(exc).__name__)
        return []
    finally:
        _close(connection)

    dated = []
    for message in found:
        try:
            sent = parsedate_to_datetime(str(message["date"]))
        except (TypeError, ValueError):
            continue
        sent = sent if sent.tzinfo else sent.replace(tzinfo=timezone.utc)
        # The search matches any text in From, so notacme.com or a display name still gets checked out here.
        if sent >= since and sent_by(str(message["from"]), domains):
            dated.append((sent, message))
    return [message for _, message in sorted(dated, key=lambda item: item[0], reverse=True)]
