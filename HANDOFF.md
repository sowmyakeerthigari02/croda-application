# ApplyPilot handoff

Paste this into a new Claude session together with the code (a GitHub repo or zip of this folder).

## What it is
ApplyPilot is a job-application assistant (originally planned in a ChatGPT chat; "Croda" was one of the first
employers it was tested against). A person pastes a job link and uploads the resume for that job; ApplyPilot
opens the employer's site in a real browser (Playwright), fills the form from the resume and a saved profile,
asks numbered questions only for answers it does not know, and submits after approval. It is marked Applied only
after the employer's confirmation page.

## Stack
- Python 3.11, FastAPI (`app/main.py`), single-page dashboard (`app/static/index.html`).
- Playwright Chromium worker on its own thread (`app/worker.py`), form scanning/filling (`app/forms.py`),
  answer rules (`app/rules.py`).
- Storage: SQLite locally or Supabase Postgres (`DATABASE_URL`); resumes in local files or a private Supabase bucket.
- Encrypted password vault for employer accounts (`app/vault.py`), connected Gmail/Yahoo/iCloud mailbox via IMAP app
  password for emailed codes and verification links (`app/mailbox.py`), cover letters (`app/letters.py`).
- Optional Claude API for job analysis/letters (`app/ai.py`); works without a key.
- Deploy files: `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `deploy/` (DigitalOcean droplet planned).
- Tests: `pytest` (about 95 tests, a local fixture employer site in `tests/fixture_site.py`).

## Run locally
```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt && .venv/Scripts/playwright install chromium
.venv/Scripts/python -m uvicorn app.main:app --port 8000
.venv/Scripts/python -m pytest -q
```
All POST requests need the header `X-ApplyPilot: 1`.

## The applicant's standing rules
- Use only the resume sent for that job, plus the saved profile and earlier answers.
- Salary: the posted range if there is one, otherwise the profile minimum.
- Cover letter only when the job requires one.
- Prefer "apply with resume / autofill" over manual entry.
- Submit without asking when every answer is known; pause only for missing answers.
- Sponsorship: plain question -> "No". A question that itself counts OPT/STEM-OPT/F-1 as sponsorship -> the truthful
  answer. Never pause or skip a job over sponsorship; keep applying even if the employer says it won't sponsor.
- Never tick optional boxes: future-job contact, marketing, alerts, SMS. Tick only consents required to submit.
- Leave optional self-ID blank unless told otherwise.
- Never enter false information (e.g. an address where the applicant does not live).
- CAPTCHAs and emailed codes are done by the person (or by the connected mailbox for codes).

## Known problems still open
- Greenhouse: newer react-select dropdowns are not filled; the submit step clicked the top "Apply" button instead
  of "Submit application"; resume upload sometimes fails in the automated browser.
- Workday: multi-select pickers ("How did you hear") are not filled; account creation is blocked on some sites
  (Yale); a pending password was deleted after the person created the account by hand.
- Paylocity: the parser puts the applicant's own phone/address/salary into employer work-history fields.
- Lever/hCaptcha and Workday bot checks often fail in the automated browser; the person's own browser passes.

## Fixed in the last session (all tested)
Emailed verification links; file-picker uploads; idle browser kept open while the person works; invisible reCAPTCHA
badge no longer treated as a CAPTCHA; checkbox options containing commas; Workday sign-in (switches from Create
Account to the Sign In pop-up and finds its email box).

## Where applications stand (Nov 2026)
Submitted and confirmed: Winter Park, ampliFI Loyalty Solutions, KU Endowment, Strive Pharmacy; Service Wire
(submitted by the applicant). Skipped: Merrick (US citizenship required), KU Health System (sign-in failed),
Ateeca (LinkedIn Easy Apply only). Not done: Yale (Workday account), Clover Sonoma, GDH, Custom Alarm (apply links
hidden behind LinkedIn sign-in), HR Green (ADP sign-in), WaTech (account + Cloudflare).

Secrets (keys, passwords, the applicant database in `data/`) are not part of this file and must not be pasted into
a chat.
