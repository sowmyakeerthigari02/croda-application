"""Cover letters: only for jobs that require one, and built only from the resume and profile."""
import io
import re

from docx import Document
from docx.shared import Pt

from . import ai

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_NOT_REQUIRED = re.compile(r"optional|not (required|necessary|mandatory)|if you (wish|like|want)|encouraged|welcome|preferred|\bmay\b", re.I)
_REQUIRED = re.compile(r"required|requires?\b|mandatory|\bmust\b|need to|to be considered|(include|submit|attach|provide|upload)", re.I)
_COVER_FIELD = re.compile(r"\bcover(ing)? letter\b|\bmotivation letter\b", re.I)
_SUMMARY = re.compile(r"(?:professional )?summary\s*\n(.+?)\n\s*(?:technical skills|skills|work experience|experience|education)\b",
                      re.I | re.S)


def required_by_posting(text: str) -> bool:
    """True only when a sentence about the cover letter says it is needed, not merely welcome."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if re.search(r"\bcover(ing)? letter\b", sentence, re.I):
            if not _NOT_REQUIRED.search(sentence) and _REQUIRED.search(sentence):
                return True
    return False


def is_cover_field(label: str) -> bool:
    return bool(_COVER_FIELD.search(label or ""))


def _job_title(job: dict) -> str:
    return re.split(r"\s+\||\s+at\s+", job.get("title") or "this position")[0].strip() or "this position"


def _employer(job: dict) -> str:
    return (job.get("company") or "").strip() or "your organization"


def write(profile: dict, job: dict, resume_text: str) -> str:
    """Claude's draft when available. Otherwise a plain letter using the resume's own summary."""
    name = " ".join(p for p in (profile.get("first_name"), profile.get("last_name")) if p)
    body = ai.draft_cover_letter(job, resume_text, name)
    if not body:
        summary = _SUMMARY.search(resume_text or "")
        background = " ".join(summary.group(1).split()) if summary else ""
        paragraphs = [f"Dear Hiring Team,",
                      f"I am applying for the {_job_title(job)} position at {_employer(job)}. My resume is attached."]
        if background:
            paragraphs.append(f"A summary of my background: {background}")
        paragraphs += ["I would welcome the chance to discuss how my experience fits this role. "
                       "Thank you for your consideration.", f"Sincerely,\n{name}"]
        body = "\n\n".join(paragraphs)
    contact = " | ".join(p for p in (profile.get("phone"), profile.get("email")) if p)
    return f"{name}\n{contact}\n\n{body}" if contact else f"{name}\n\n{body}"


def to_docx(text: str) -> bytes:
    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    for paragraph in text.split("\n\n"):
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def filename(profile: dict) -> str:
    parts = [re.sub(r"[^\w-]", "", p) for p in (profile.get("first_name"), profile.get("last_name")) if p]
    return "_".join(parts + ["Cover_Letter"]) + ".docx"
