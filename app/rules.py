"""Turns a form field plus the saved profile into an answer, without guessing.

Anything these rules cannot settle becomes a numbered question for the person.
"""
import re
from datetime import date, timedelta
from urllib.parse import urlparse

LEAVE_BLANK = "__LEAVE_BLANK__"

PERSONAL_FIELDS = [
    "first_name", "last_name", "preferred_name", "email", "phone", "address",
    "city", "state", "zip", "country", "linkedin", "website",
]
JOB_DATA_FIELDS = [
    "authorized_to_work", "needs_sponsorship", "visa_status", "over_18", "min_salary",
    "salary_offset", "bonus_expectation", "notice_period", "willing_to_relocate",
    "onsite_preference", "how_heard", "gender", "race", "hispanic", "veteran", "disability",
]

# Ordered: earlier patterns win, so "preferred name" never lands on "first name".
CLASSIFIERS: list[tuple[str, str]] = [
    ("preferred_name", r"preferred (first )?name|nickname|known as"),
    ("last_name", r"\b(sur ?name|last name|family name)\b"),
    ("first_name", r"\b(first name|given name|forename)\b"),
    ("full_name", r"^(your )?(full |legal )?name$|\b(full|legal) name\b"),
    ("email", r"\be ?mail\b"),
    ("phone", r"\b(phone|mobile|telephone|cell)\b(?!.*\b(extension|type|code)\b)"),
    ("linkedin", r"\blinkedin\b(?!.*\bhear\b)"),
    ("website", r"\b(website|portfolio|github|personal url)\b"),
    ("needs_sponsorship", r"\bsponsor"),
    ("authorized_to_work", r"\b(authori[sz]ed|eligible|right|permitted) to work\b"),
    ("visa_status", r"\b(visa|immigration) (status|type)\b|\bwork permit\b"),
    ("over_18", r"\b(18 years|over 18|at least 18|legal age)\b"),
    ("bonus_expectation", r"\bbonus\b"),
    ("salary", r"\b(salary|compensation|pay expectation|desired pay)\b"),
    ("notice_period", r"\b(notice period|start date|available to start|availability date|when can you start)\b"),
    ("willing_to_relocate", r"\brelocat"),
    ("onsite_preference", r"\b(on ?site|hybrid|work arrangement|remote preference)\b"),
    ("how_heard", r"\b(how did you (hear|find)|where did you (hear|find|see)|job source|referral source|^source$)"),
    ("gender", r"\b(gender|sex)\b"),
    ("hispanic", r"\b(hispanic|latino)\b"),
    ("race", r"\b(race|ethnicity|ethnic)\b"),
    ("veteran", r"\bveteran\b"),
    ("disability", r"\bdisab"),
    ("zip", r"\b(zip|postal|post code|postcode)\b"),
    ("city", r"\b(city|town)\b"),
    ("state", r"\b(state|province)\b"),
    ("country", r"^country\b|\bcountry of residence\b"),
    ("address", r"\b(street|address)\b"),
]

_CONSENT = re.compile(r"\b(i agree|agree to|i certify|i declare|declaration|i acknowledge|i consent|true and accurate|terms)\b", re.I)
_DECLINE = ("do not wish", "dont wish", "don t wish", "decline", "prefer not", "choose not",
            "not to disclose", "not to answer", "not to say", "not wish to")
_NEGATIONS = {"not", "no", "non", "never"}


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def question_key(label: str) -> str:
    return norm(label)[:200]


def is_consent(label: str) -> bool:
    return bool(_CONSENT.search(label or ""))


def classify(label: str, kind: str = "text") -> str | None:
    text = norm(label)
    if not text or kind == "textarea" or len(text) > 140:
        return None
    for key, pattern in CLASSIFIERS:
        if re.search(pattern, text):
            return key
    return None


def _has_words(haystack: str, needle: str) -> bool:
    return bool(needle) and re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


def match_option(answer: str, options: list[dict]) -> dict | None:
    """Return the single option this answer clearly means, or None. Never a best guess."""
    wanted = norm(answer)
    if not wanted:
        return None
    labelled = [(norm(o["label"]), o) for o in options]
    for text, option in labelled:
        if text == wanted:
            return option
    if any(d in wanted for d in _DECLINE):
        hits = [o for text, o in labelled if any(d in text for d in _DECLINE)]
        return hits[0] if len(hits) == 1 else None
    hits = []
    for text, option in labelled:
        if _has_words(text, wanted):
            hits.append(option)
        elif _has_words(wanted, text):
            extra = set(wanted.split()) - set(text.split())
            if not extra & _NEGATIONS:
                hits.append(option)
    return hits[0] if len(hits) == 1 else None


def extract_salary(text: str) -> tuple[int | None, int | None]:
    """Read an advertised USD salary or range from a posting."""
    found = []
    for match in re.finditer(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}\s?[kK])\b", text or ""):
        raw = match.group(1).replace(",", "").replace(" ", "")
        value = int(raw[:-1]) * 1000 if raw[-1] in "kK" else int(raw)
        if 20_000 <= value <= 1_000_000:
            found.append(value)
    if not found:
        return None, None
    return min(found[:2]), max(found[:2])


def salary_value(profile: dict, job: dict, kind: str) -> str | None:
    """A posted range wins: its top minus the offset (default 5,000), staying inside the range.

    With no posted salary, the person's own figure. Digits only for short fields, because portals
    often reject anything else.
    """
    top = job.get("salary_max") or job.get("salary_min")
    if top:
        offset = _digits(profile.get("salary_offset")) or 5000
        value = max(int(job.get("salary_min") or 0), int(top) - offset)
    else:
        value = _digits(profile.get("min_salary"))
        if value is None:
            return None
    return f"USD {value:,} per year" if kind == "textarea" else str(value)


def _digits(value) -> int | None:
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else None


def notice_days(period: str) -> int | None:
    match = re.search(r"(\d+)\s*(day|week|month)", (period or "").lower())
    if not match:
        return None
    return int(match.group(1)) * {"day": 1, "week": 7, "month": 30}[match.group(2)]


def availability_date(period: str, today: date) -> date | None:
    """The application date counts as day 1. A weekend result moves to the next Monday."""
    days = notice_days(period)
    if days is None:
        return None
    result = today + timedelta(days=days - 1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def resolve(field: dict, profile: dict, job: dict, job_url: str, today: date | None = None) -> str | None:
    """The answer the saved profile gives for this field, or None when it gives none."""
    today = today or date.today()
    kind, label = field.get("kind", "text"), field.get("label", "")
    key = classify(label, kind)
    if key is None:
        return None
    if key == "full_name":
        return " ".join(p for p in (profile.get("first_name"), profile.get("last_name")) if p) or None
    if key == "salary":
        return salary_value(profile, job, kind)
    if key == "notice_period":
        period = profile.get("notice_period")
        if not period:
            return None
        if kind == "date" or "date" in norm(label).split():
            when = availability_date(period, today)
            if when is None:
                return None
            return when.isoformat() if kind == "date" else when.strftime("%m/%d/%Y")
        return period
    if key == "how_heard" and not profile.get("how_heard"):
        return "LinkedIn" if "linkedin." in urlparse(job_url).netloc.lower() else None
    return profile.get(key) or None


def question_for(field: dict, number_key: str | None = None, note: str = "") -> dict:
    """A question in the format the person asked for: options listed, or a hint and an example."""
    options = [o["label"] for o in field.get("options", [])]
    kind = field.get("kind", "text")
    if kind == "checkbox":
        options = ["Yes", "No"]
    return {
        "key": number_key or question_key(field["label"]),
        "question": field["label"],
        "kind": "choice" if options else "text",
        "options": options,
        "hint": note or ("" if options else _hint(field)),
        "example": "" if options else _example(field),
        "required": bool(field.get("required")),
        "answer": None,
    }


def _hint(field: dict) -> str:
    key = classify(field["label"], field.get("kind", "text"))
    return {
        "salary": "Your salary expectation as a number, with the currency.",
        "notice_period": "How much notice you must give, or the date you can start.",
        "preferred_name": "The name you would like the employer to use.",
    }.get(key, "Type the answer exactly as it should appear in this field.")


def _example(field: dict) -> str:
    key = classify(field["label"], field.get("kind", "text"))
    return {
        "salary": "USD 145,000 per year",
        "notice_period": "2 weeks",
        "preferred_name": "Sam",
        "last_name": "Taylor",
        "phone": "+1 555 010 0199",
        "city": "Chicago",
    }.get(key, "Leave blank" if not field.get("required") else "A short, factual answer")


def parse_numbered_reply(text: str, count: int) -> dict[int, str]:
    """Parse '1. No, 2. Leave blank, 3. USD 145,000 per year' into {1: 'No', ...}."""
    marks = [m for m in re.finditer(r"(?:^|(?<=[\s,;]))(\d{1,2})[.):]\s+", text or "")
              if 1 <= int(m.group(1)) <= count]
    out: dict[int, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(mark.group(1))] = text[mark.end():end].strip(" \t\r\n,;")
    return out


def normalize_answer(question: dict, value: str) -> str:
    """Validate one reply against its question. Raises ValueError with a message to show."""
    value = (value or "").strip()
    if not value:
        raise ValueError(f"No answer given for: {question['question']}")
    if norm(value) == "leave blank":
        return LEAVE_BLANK
    if question["options"]:
        options = [{"label": o} for o in question["options"]]
        match = match_option(value, options)
        if match is None:
            raise ValueError(
                f"\"{value}\" is not one of the options for: {question['question']}. "
                f"Options: {' / '.join(question['options'])}"
            )
        return match["label"]
    return value
