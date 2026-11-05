"""HTTP API and dashboard."""
import asyncio
import base64
import io
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import ai, config, db, mailbox, rules, storage, vault
from .worker import is_public_url, runner

logging.basicConfig(level=logging.INFO)

STATIC = Path(__file__).parent / "static"
MAX_RESUME_BYTES = 10 * 1024 * 1024
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SECTIONS = {
    "new": ("NEW", "ANALYZING", "APPLYING"),
    "paused": ("PAUSED",),
    "applied": ("SUBMITTED",),
    "saved": ("SAVED",),
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    storage.store()
    # A restart loses open browsers. Answers are saved, so these can simply be started again.
    db.execute(
        "UPDATE applications SET status='PAUSED', pause_kind='help', questions='[]', "
        "pause_reason='The server restarted. Retry to continue; your answers are saved.' "
        "WHERE status IN ('NEW','ANALYZING','APPLYING') OR (status='PAUSED' AND pause_kind!='help')"
    )
    runner.start()
    yield
    runner.stop()
    db.close()


app = FastAPI(title="ApplyPilot", lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    if config.APP_PASSWORD and not _password_ok(request.headers.get("authorization", "")):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="ApplyPilot"'})
    # Browsers replay Basic credentials on cross-site requests; other sites cannot set this header.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-applypilot") != "1":
        return JSONResponse({"detail": "Missing request header."}, status_code=403)
    return await call_next(request)


def _password_ok(header: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode()
    except ValueError:
        return False
    return secrets.compare_digest(decoded.partition(":")[2].encode(), config.APP_PASSWORD.encode())


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# Applicants and profile

def _applicant(applicant_id: str) -> dict:
    row = db.one("SELECT id, name, profile, created_at, browser_session IS NOT NULL AS signed_in "
                 "FROM applicants WHERE id=?", applicant_id)
    if not row:
        raise HTTPException(404, "Applicant not found.")
    return row


@app.get("/api/meta")
def meta():
    return {"ai_enabled": ai.available(), "personal_fields": rules.PERSONAL_FIELDS,
            "job_data_fields": rules.JOB_DATA_FIELDS}


@app.get("/api/applicants")
def list_applicants():
    return db.query("SELECT id, name, profile, created_at, browser_session IS NOT NULL AS signed_in "
                    "FROM applicants ORDER BY created_at")


@app.post("/api/applicants")
async def create_applicant(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "A name is required.")
    applicant_id = db.new_id()
    db.insert("applicants", id=applicant_id, name=name, profile={}, created_at=db.now())
    return _applicant(applicant_id)


@app.put("/api/applicants/{applicant_id}")
async def update_applicant(applicant_id: str, request: Request):
    _applicant(applicant_id)
    body = await request.json()
    allowed = set(rules.PERSONAL_FIELDS) | set(rules.JOB_DATA_FIELDS)
    profile = {k: str(v).strip() for k, v in (body.get("profile") or {}).items() if k in allowed and str(v).strip()}
    name = " ".join(p for p in (profile.get("first_name"), profile.get("last_name")) if p)
    db.execute("UPDATE applicants SET profile=?, name=COALESCE(NULLIF(?,''),name) WHERE id=?",
               db._encode("profile", profile), name, applicant_id)
    return _applicant(applicant_id)


@app.get("/api/applicants/{applicant_id}/answers")
def list_answers(applicant_id: str):
    return db.query("SELECT key, question, answer, updated_at FROM saved_answers WHERE applicant_id=? "
                    "ORDER BY updated_at DESC", applicant_id)


@app.delete("/api/applicants/{applicant_id}/answers/{key}")
def delete_answer(applicant_id: str, key: str):
    db.execute("DELETE FROM saved_answers WHERE applicant_id=? AND key=?", applicant_id, key)
    return {"ok": True}


# Saved site accounts. Passwords go in and never come back out through the API.

@app.get("/api/applicants/{applicant_id}/credentials")
def list_credentials(applicant_id: str):
    _applicant(applicant_id)
    return vault.list_for(applicant_id)


@app.post("/api/applicants/{applicant_id}/credentials")
async def add_credential(applicant_id: str, request: Request):
    _applicant(applicant_id)
    body = await request.json()
    site = vault.site_key((body.get("site") or "").strip())
    login_email = (body.get("login_email") or "").strip()
    password = body.get("password") or ""
    if not site or not login_email or not password:
        raise HTTPException(422, "Enter the site, the login email and the password.")
    vault.save(applicant_id, site, login_email, password)
    return vault.list_for(applicant_id)


@app.delete("/api/applicants/{applicant_id}/credentials/{credential_id}")
def delete_credential(applicant_id: str, credential_id: str):
    vault.delete(applicant_id, credential_id)
    return {"ok": True}


@app.get("/api/applicants/{applicant_id}/auto-accounts")
def get_auto_accounts(applicant_id: str):
    _applicant(applicant_id)
    return {"since": vault.auto_accounts_since(applicant_id)}


@app.put("/api/applicants/{applicant_id}/auto-accounts")
async def set_auto_accounts(applicant_id: str, request: Request):
    _applicant(applicant_id)
    vault.set_auto_accounts(applicant_id, (await request.json()).get("enabled") is True)
    return {"since": vault.auto_accounts_since(applicant_id)}


# The connected email, for verification codes. Its app password never comes back out either.

@app.get("/api/applicants/{applicant_id}/mailbox")
def get_mailbox(applicant_id: str):
    _applicant(applicant_id)
    return {"mailbox": mailbox.get(applicant_id)}


@app.put("/api/applicants/{applicant_id}/mailbox")
async def connect_mailbox(applicant_id: str, request: Request):
    _applicant(applicant_id)
    body = await request.json()
    address = (body.get("address") or "").strip()
    password = body.get("app_password") or ""
    if not address or not password:
        raise HTTPException(422, "Enter the email address and its app password.")
    try:
        await asyncio.to_thread(mailbox.connect, applicant_id, address, password)
    except mailbox.MailboxError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"mailbox": mailbox.get(applicant_id)}


@app.delete("/api/applicants/{applicant_id}/mailbox")
def disconnect_mailbox(applicant_id: str):
    mailbox.disconnect(applicant_id)
    return {"ok": True}


# Resumes

async def _store_resume(applicant_id: str, upload: UploadFile) -> dict:
    filename = re.sub(r"[^\w.\- ()]", "_", Path(upload.filename or "resume").name)
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".docx"):
        raise HTTPException(422, "Please upload a PDF or DOCX resume.")
    data = await upload.read(MAX_RESUME_BYTES + 1)
    if len(data) > MAX_RESUME_BYTES:
        raise HTTPException(422, "The resume must be 10 MB or smaller.")
    resume_id = db.new_id()
    # The original name stays in the database, because employers see it; storage keys stay plain.
    key = f"resumes/{resume_id}/resume{suffix}"
    content_type = "application/pdf" if suffix == ".pdf" else DOCX_MIME
    try:
        await asyncio.to_thread(storage.store().put, key, data, content_type)
    except storage.StorageError as exc:
        raise HTTPException(502, "The resume could not be saved. Please try again.") from exc
    db.insert("resumes", id=resume_id, applicant_id=applicant_id, filename=filename, path=key,
              text=_resume_text(data, suffix), created_at=db.now())
    return _resume_row(resume_id)


def _resume_text(data: bytes, suffix: str) -> str:
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        from docx import Document
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    except Exception as exc:  # a malformed file still works as an upload; only drafting loses out
        logging.getLogger("applypilot").warning("Could not read resume text: %s", type(exc).__name__)
        return ""


def _resume_row(resume_id: str) -> dict:
    return db.one("SELECT id, applicant_id, filename, created_at, length(text) AS text_length "
                  "FROM resumes WHERE id=?", resume_id)


@app.get("/api/applicants/{applicant_id}/resumes")
def list_resumes(applicant_id: str):
    return db.query("SELECT id, applicant_id, filename, created_at, length(text) AS text_length "
                    "FROM resumes WHERE applicant_id=? ORDER BY created_at DESC", applicant_id)


@app.post("/api/applicants/{applicant_id}/resumes")
async def upload_resume(applicant_id: str, file: UploadFile = File(...)):
    _applicant(applicant_id)
    return await _store_resume(applicant_id, file)


@app.delete("/api/resumes/{resume_id}")
def delete_resume(resume_id: str):
    row = db.one("SELECT path FROM resumes WHERE id=?", resume_id)
    if not row:
        raise HTTPException(404, "Resume not found.")
    if db.one("SELECT id FROM applications WHERE resume_id=? LIMIT 1", resume_id):
        raise HTTPException(409, "An application uses this resume, so it cannot be deleted.")
    db.execute("DELETE FROM resumes WHERE id=?", resume_id)
    storage.store().delete(row["path"])
    return {"ok": True}


# Applications

def _application(app_id: str) -> dict:
    row = db.get_app(app_id)
    if not row:
        raise HTTPException(404, "Application not found.")
    resume = db.one("SELECT filename FROM resumes WHERE id=?", row["resume_id"])
    row["resume_filename"] = resume["filename"] if resume else ""
    row["live"] = runner.is_live(app_id)
    row["answers"] = {k: ("Leave blank" if v == rules.LEAVE_BLANK else v) for k, v in row["answers"].items()}
    return row


@app.get("/api/applications")
def list_applications(applicant_id: str):
    rows = db.query("SELECT id FROM applications WHERE applicant_id=? ORDER BY updated_at DESC", applicant_id)
    apps = [_application(r["id"]) for r in rows]
    counts = {name: sum(a["status"] in statuses for a in apps) for name, statuses in SECTIONS.items()}
    return {"applications": apps, "counts": counts}


@app.post("/api/applications")
async def create_application(
    applicant_id: str = Form(...),
    job_url: str = Form(...),
    resume_id: str = Form(""),
    start: bool = Form(True),
    file: UploadFile | None = File(None),
):
    _applicant(applicant_id)
    job_url = job_url.strip()
    if not is_public_url(job_url):
        raise HTTPException(422, "That does not look like a reachable job posting URL.")
    if file is not None and file.filename:
        resume_id = (await _store_resume(applicant_id, file))["id"]
    if not db.one("SELECT id FROM resumes WHERE id=? AND applicant_id=?", resume_id, applicant_id):
        raise HTTPException(422, "Upload a resume or select an existing one.")
    app_id = db.new_id()
    now = db.now()
    db.insert("applications", id=app_id, applicant_id=applicant_id, job_url=job_url, resume_id=resume_id,
              status="NEW" if start else "SAVED", started_at=now, updated_at=now,
              job={}, questions=[], answers={}, log=[{"at": now, "message": "Application created."}])
    if start:
        runner.enqueue(app_id)
    return _application(app_id)


@app.get("/api/applications/{app_id}")
def get_application(app_id: str):
    return _application(app_id)


@app.get("/api/applications/{app_id}/cover-letter")
def download_cover_letter(app_id: str):
    letter = _application(app_id)["job"].get("cover_letter")
    if not letter:
        raise HTTPException(404, "This application has no cover letter.")
    media = "application/pdf" if letter["filename"].lower().endswith(".pdf") else DOCX_MIME
    headers = {"Content-Disposition": f'attachment; filename="{letter["filename"]}"', "Cache-Control": "no-store"}
    return Response(storage.store().get(letter["path"]), media_type=media, headers=headers)


@app.post("/api/applications/{app_id}/cover-letter")
async def upload_cover_letter(app_id: str, file: UploadFile = File(...)):
    """Use the person's own letter for this application instead of a generated one."""
    row = _application(app_id)
    name = re.sub(r"[^\w.\- ()]", "_", Path(file.filename or "cover_letter").name)
    suffix = Path(name).suffix.lower()
    if suffix not in (".pdf", ".docx"):
        raise HTTPException(422, "Please upload a PDF or DOCX cover letter.")
    data = await file.read(MAX_RESUME_BYTES + 1)
    if len(data) > MAX_RESUME_BYTES:
        raise HTTPException(422, "The cover letter must be 10 MB or smaller.")
    path = f"cover_letters/{app_id}/letter{suffix}"
    await asyncio.to_thread(storage.store().put, path, data, "application/pdf" if suffix == ".pdf" else DOCX_MIME)
    job = row["job"]
    job["cover_letter"] = {"path": path, "filename": name, "text": _resume_text(data, suffix)}
    db.update_app(app_id, job=job)
    db.add_log(app_id, f"Your cover letter {name} will be used for this application.")
    return _application(app_id)


@app.post("/api/applications/{app_id}/start")
def start_application(app_id: str):
    row = _application(app_id)
    if row["status"] == "SUBMITTED":
        raise HTTPException(409, "This application was already submitted.")
    db.update_app(app_id, status="NEW", pause_kind="", pause_reason="", questions=[], submission_status="Not submitted")
    runner.resume(app_id)
    return _application(app_id)


@app.post("/api/applications/{app_id}/answers")
async def answer_questions(app_id: str, request: Request):
    """Takes {"answers": {key: value}} or a one-line {"reply": "1. No, 2. Leave blank"}."""
    row = _application(app_id)
    questions = row["questions"]
    if row["pause_kind"] != "questions" or not questions:
        raise HTTPException(409, "This application is not waiting for answers.")
    body = await request.json()
    given: dict[str, str] = dict(body.get("answers") or {})
    if body.get("reply"):
        parsed = rules.parse_numbered_reply(body["reply"], len(questions))
        if not parsed:
            raise HTTPException(422, 'Number each answer, for example: "1. No, 2. Leave blank".')
        for question in questions:
            if question["number"] in parsed:
                given[question["key"]] = parsed[question["number"]]

    raw = db.get_app(app_id)
    answers = raw["answers"]
    errors = []
    for question in questions:
        if question["key"] not in given:
            continue
        try:
            value = rules.normalize_answer(question, given[question["key"]])
        except ValueError as exc:
            errors.append(str(exc))
            continue
        question["answer"] = value
        answers[question["key"]] = value
        # Consent is given per employer and per application, so it is never reused. Nor are labels
        # that mean something different each time, such as one job's company or a date part.
        if not rules.is_consent(question["question"]) and question["key"] not in ("company", "month", "day", "year"):
            db.save_answer(row["applicant_id"], question["key"], question["question"], value)
    db.update_app(app_id, answers=answers, questions=questions)
    if errors:
        raise HTTPException(422, " ".join(errors))
    if all(q["answer"] is not None for q in questions):
        db.add_log(app_id, "Your answers were received. Continuing.")
        runner.resume(app_id)
    return _application(app_id)


@app.post("/api/applications/{app_id}/decision")
async def decide(app_id: str, request: Request):
    """The person's yes or no at the final step or on a new account. Only a live page can be submitted."""
    row = _application(app_id)
    decision = (await request.json()).get("decision")
    if row["pause_kind"] == "account" and decision in ("create", "self"):
        db.update_app(app_id, decision=decision)
        runner.resume(app_id)
        return _application(app_id)
    if row["pause_kind"] != "approval" or decision not in ("submit", "hold"):
        raise HTTPException(409, "This application is not waiting for a decision.")
    if not row["live"] and decision == "submit":
        db.add_log(app_id, "The browser had been released, so the form is being filled again. You will be asked again before submitting.")
    db.update_app(app_id, decision=decision if row["live"] else "")
    if not row["live"] and decision == "hold":
        db.update_app(app_id, status="SAVED", pause_kind="", pause_reason="", submission_status="Held by you, not submitted")
        return _application(app_id)
    runner.resume(app_id)
    return _application(app_id)


@app.post("/api/applications/{app_id}/continue")
def continue_application(app_id: str):
    """After the person has finished in the live browser, or to retry from the job link."""
    row = _application(app_id)
    if row["status"] != "PAUSED":
        raise HTTPException(409, "This application is not paused.")
    runner.resume(app_id)
    return _application(app_id)


@app.post("/api/applications/{app_id}/mark-applied")
def mark_applied(app_id: str):
    """For the case where the person saw the employer's confirmation themselves."""
    _application(app_id)
    db.update_app(app_id, status="SUBMITTED", progress=100, pause_kind="", pause_reason="", questions=[],
                  submission_status="Marked as submitted by you")
    db.add_log(app_id, "You marked this application as submitted.")
    return _application(app_id)


@app.get("/api/applications/{app_id}/screenshot")
def screenshot(app_id: str):
    row = _application(app_id)
    if not row["screenshot"]:
        raise HTTPException(404, "No screenshot yet.")
    try:
        image = storage.store().get(row["screenshot"])
    except storage.StorageError as exc:
        raise HTTPException(404, "No screenshot yet.") from exc
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/applications/{app_id}/live.jpg")
def live_image(app_id: str):
    frame = runner.live_frame(app_id)
    if frame is None:
        raise HTTPException(404, "No live browser for this application.")
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/applications/{app_id}/live/input")
async def live_input(app_id: str, request: Request):
    if not runner.live_input(app_id, await request.json()):
        raise HTTPException(409, "The live browser only takes input while the application is paused.")
    return {"ok": True}
