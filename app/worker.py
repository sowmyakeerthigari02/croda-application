"""The browser worker: drives one application at a time per job, pausing whenever a person is needed.

An application is only ever marked SUBMITTED after the employer's page confirms it, and the
submit control is only ever clicked after the person approves that exact submission.
"""
import asyncio
import ipaddress
import json
import logging
import re
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from cryptography.fernet import InvalidToken
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from . import ai, config, db, forms, letters, mailbox, rules, storage, vault

log = logging.getLogger("applypilot.worker")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_STEPS = 40
MAX_APPLY_CLICKS = 3
MAX_REFILLS = 2
MAX_LOAD_WAITS = 6
MAX_CODE_TRIES = 2
# Personal accounts, not employer portals: the person signs in to these, and new ones are never made.
NEVER_NEW_ACCOUNT = {"linkedin.com", "indeed.com", "glassdoor.com", "google.com", "microsoft.com", "facebook.com", "apple.com"}
CODE_LOOKBACK = timedelta(minutes=2)  # the email can be sent just before the code page finishes loading
# What a file picker's box is for: its label, name and the text right around it. A whole form's text
# would mention the cover letter even next to the resume box, so it is not used.
_FILE_LABEL_JS = """e => [[...(e.labels || [])].map(l => l.innerText).join(' '), e.name, e.id, e.getAttribute('aria-label'),
    ((e.parentElement || {}).innerText || '').slice(0, 200)].join(' ')"""


class _Finished(Exception):
    """The job ended on purpose and its status is already saved."""


class _Expired(Exception):
    """Nobody answered in time. The browser is released and the answers stay saved."""


def is_public_url(url: str) -> bool:
    """Blocks internal addresses, such as the Droplet's metadata service."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if config.ALLOW_PRIVATE_URLS:
        return True
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


class Job:
    def __init__(self, runner: "Runner", app_id: str):
        self.runner = runner
        self.id = app_id
        self.wake = asyncio.Event()
        self.paused = False
        self.last_input = 0.0  # when the person last clicked or typed in the live browser
        self.context = None
        self._page = None
        self.form_seen = False
        self.apply_clicks = 0
        self.refills = 0
        self.load_waits = 0
        self.holds_slot = False
        self.uploaded: set[str] = set()
        self.submit_clicked = False
        self.resume_sent = False
        self.letter_sent = False
        self.tried_login: set[str] = set()  # one automatic try per site, so accounts are not locked out
        self.asked_account: set[str] = set()
        self.code_tries: dict[str, int] = {}
        self.code_since: dict[str, datetime] = {}
        self.used_codes: set[str] = set()  # held in memory only, for this run
        self.used_links: set[str] = set()

    @property
    def page(self):
        if self._page is None or self._page.is_closed():
            self._page = self.context.pages[-1]
        return self._page

    def _on_new_page(self, page):
        self._page = page  # employers often open the real form in a new tab
        page.on("filechooser", self._choose_file)

    async def _choose_file(self, chooser):
        """An Upload button opened the system file picker, which no one can use in the live browser.

        The picker gets this job's resume, or its cover letter when the box is labeled for one.
        """
        try:
            label = await chooser.element.evaluate(_FILE_LABEL_JS)
        except PlaywrightError:
            label = ""
        try:
            if letters.is_cover_field(label):
                letter = await self._cover_letter()
                await chooser.set_files(letter["file"])
                self.letter_sent = True
                db.add_log(self.id, f"Uploaded cover letter {letter['file']['name']} through the site's file picker.")
                return
            upload = await self._resume_file()
            await chooser.set_files(upload)
            self.resume_sent = True
            db.add_log(self.id, f"Uploaded resume {upload['name']} through the site's file picker.")
        except PlaywrightError as exc:
            db.add_log(self.id, f"Could not upload through the site's file picker: {str(exc).splitlines()[0]}")

    async def run(self):
        await self.runner.slots.acquire()
        self.holds_slot = True
        try:
            await self._open()
            await self._analyze()
            await self._apply()
        except _Finished:
            pass
        except _Expired:
            db.add_log(self.id, "Browser released after waiting. Your answers are saved; replying restarts it.")
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("Browser failure on %s: %s", self.id, str(exc).splitlines()[0])
            db.update_app(self.id, status="PAUSED", pause_kind="help", questions=[],
                          pause_reason="The browser hit a problem on this page. Retry to start again from the job link.")
            db.add_log(self.id, f"Browser error: {str(exc).splitlines()[0]}")
        except Exception:
            # For example Supabase being unreachable. Never leave a job looking like it is running.
            log.exception("Application %s stopped unexpectedly", self.id)
            db.update_app(self.id, status="PAUSED", pause_kind="help", questions=[],
                          pause_reason="Something went wrong on the server. Retry to start again from the job link.")
        finally:
            await self._close()
            self.runner.jobs.pop(self.id, None)
            if self.holds_slot:
                self.runner.slots.release()

    async def _open(self):
        browser = await self.runner.browser()
        app = db.get_app(self.id)
        self.context = await browser.new_context(
            viewport=config.VIEWPORT, storage_state=_load_session(app["applicant_id"])
        )
        self.context.on("page", self._on_new_page)
        await self.context.route("**/*", self._guard)
        self._page = await self.context.new_page()
        self._page.on("filechooser", self._choose_file)

    async def _guard(self, route):
        request = route.request
        if request.is_navigation_request() and not await asyncio.to_thread(is_public_url, request.url):
            await route.abort()
        else:
            await route.continue_()

    async def _close(self):
        if self.context:
            try:
                await self._save_session()
                await self.context.close()
            except PlaywrightError:
                pass

    async def _save_session(self):
        state = await self.context.storage_state()
        app = db.get_app(self.id)
        db.execute("UPDATE applicants SET browser_session=? WHERE id=?",
                   config.FERNET.encrypt(json.dumps(state).encode()), app["applicant_id"])

    async def _settle(self):
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            await self.page.wait_for_load_state("networkidle", timeout=3000)
        except PlaywrightTimeout:
            pass
        await self.page.wait_for_timeout(300)

    async def _snapshot(self) -> str:
        key = f"screenshots/{self.id}.jpg"
        image = await self.page.screenshot(type="jpeg", quality=70)
        await asyncio.to_thread(storage.store().put, key, image, "image/jpeg")
        return key

    async def _pause(self, kind: str, reason: str, questions: list | None = None):
        # Nothing is saved to disk during sign-in, so passwords never reach a stored image.
        shot = "" if kind == "sign_in" else await self._snapshot()
        self.wake.clear()
        self.paused = True
        db.update_app(self.id, status="PAUSED", pause_kind=kind, pause_reason=reason, questions=questions or [],
                      decision="", current_page=self.page.url, screenshot=shot)
        db.add_log(self.id, f"Paused: {reason}")
        # A paused job is only waiting on a person, so it lets the next application run.
        self.runner.slots.release()
        self.holds_slot = False
        self.last_input = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1.5)
                break
            except asyncio.TimeoutError:
                if kind == "sign_in" and await forms.blocker(self.page) is None:
                    break
                # Someone working in the live browser keeps it open, so a half-typed page is not thrown away.
                if time.monotonic() > self.last_input + config.PAUSE_TIMEOUT_SECONDS:
                    self.paused = False
                    raise _Expired()
        self.paused = False
        await self.runner.slots.acquire()
        self.holds_slot = True
        if kind == "sign_in":
            await self._save_session()
            await self._settle()
        db.update_app(self.id, status="APPLYING", pause_kind="", pause_reason="", questions=[])

    async def _analyze(self):
        app = db.get_app(self.id)
        db.update_app(self.id, status="ANALYZING", progress=max(app["progress"], 5), pause_kind="",
                      pause_reason="", questions=[])
        db.add_log(self.id, "Opening the job posting.")
        await self.page.goto(app["job_url"], wait_until="domcontentloaded", timeout=45000)
        await self._settle()
        if not app["job"].get("analyzed"):
            text, title = await forms.page_text(self.page), await self.page.title()
            job = await asyncio.to_thread(ai.analyze_job, text, title, app["job_url"])
            job["analyzed"] = True
            db.update_app(self.id, job=job, progress=15)
            db.add_log(self.id, f"Job found: {job.get('title') or 'unknown title'} at {job.get('company') or 'unknown company'}.")
        job = db.get_app(self.id)["job"]
        if "cover_letter_required" not in job:
            job["cover_letter_required"] = letters.required_by_posting(await forms.page_text(self.page))
            db.update_app(self.id, job=job)
            if job["cover_letter_required"]:
                db.add_log(self.id, "The posting requires a cover letter.")
        db.update_app(self.id, status="APPLYING", current_page=self.page.url)

    async def _apply(self):
        for step in range(MAX_STEPS):
            await self._settle()
            db.update_app(self.id, current_page=self.page.url,
                          progress=max(db.get_app(self.id)["progress"], min(90, 20 + step * 5)))

            # After submitting, a portal's offer of an account can share the page with its thanks.
            if self.submit_clicked and await forms.application_received(self.page):
                await self._confirm()

            block = await forms.blocker(self.page)
            # Code pages often sit under /login, so they come before the sign-in check.
            if block != "captcha" and await self._emailed_code():
                continue
            if block:
                await self._sign_in_wall(block)
                continue

            fields = await forms.scan(self.page)
            actions = await forms.find_actions(self.page)

            if self.form_seen and not actions["next"] and not actions["submit"] and not fields:
                if await forms.is_confirmation(self.page):
                    await self._confirm()
                if await self._still_loading():
                    continue
                await self._pause("help", "This page has no form and no confirmation. Check it in the live browser, then continue.")
                continue

            if not self.form_seen and not _looks_like_form(fields, actions):
                if actions["apply"] and self.apply_clicks < MAX_APPLY_CLICKS:
                    self.apply_clicks += 1
                    db.add_log(self.id, f"Clicking \"{actions['apply']['text']}\".")
                    try:
                        await forms.click(self.page, actions["apply"])
                    except PlaywrightTimeout:
                        # Something covers the button, so the person takes over with the page still open.
                        await self._pause("sign_in" if await forms.blocker(self.page) else "help",
                                          "Something on this page is covering the Apply button. Clear it in the live browser, then continue.")
                        continue
                    await self.page.wait_for_timeout(1200)
                    continue
                if await self._still_loading():
                    continue
                await self._pause("help", "No application form or Apply button found. Open it in the live browser, then continue.")
                continue
            self.form_seen = True
            self.load_waits = 0

            try:
                questions = await self._fill(fields)
            except PlaywrightError as exc:
                # One awkward field should not throw away the rest of a filled-in form.
                db.add_log(self.id, f"Could not fill a field: {str(exc).splitlines()[0]}")
                await self._pause("help", "A field on this page could not be filled. Complete it in the live browser, then continue.")
                continue
            if questions:
                await self._pause("questions", "Some answers are needed to continue.", questions)
                continue

            if actions["next"]:
                await self._advance(fields, actions["next"])
            elif actions["submit"]:
                await self._approve_and_submit(fields, actions["submit"])
            elif not await self._still_loading():  # a portal disables its buttons while it saves a step
                await self._pause("help", "No Next or Submit button found. Use the live browser, then continue.")
        await self._pause("help", "This application has more steps than expected. Finish it in the live browser.")

    async def _still_loading(self) -> bool:
        """Portals such as Workday draw their forms slowly, so an empty page gets a few more looks."""
        if self.load_waits >= MAX_LOAD_WAITS:
            return False
        self.load_waits += 1
        await self.page.wait_for_timeout(2000)
        return True

    async def _emailed_code(self) -> bool:
        """Types a code from the site's own email. True when the page was handled here.

        On a page that also holds the application itself, the code is typed but nothing is clicked,
        so the usual approval still comes before anything is submitted.
        """
        fields = await forms.scan(self.page)
        boxes = forms.code_boxes(fields)
        if not boxes:
            return False
        ids = {box["id"] for box in boxes}
        declaration = forms.declaration_text(await forms.page_text(self.page))
        own_page = len([f for f in fields if f["id"] not in ids]) <= 1 and not declaration
        if not own_page and all(box["value"] for box in boxes):
            return False

        url = self.page.url
        site = vault.site_key(url)
        applicant_id = db.get_app(self.id)["applicant_id"]
        connected = mailbox.get(applicant_id)
        same_site = mailbox.registrable_domain(urlparse(forms.field_url(self.page, boxes[0])).hostname or "") \
            == mailbox.registrable_domain(urlparse(url).hostname or "")
        tries = self.code_tries.get(site, 0)
        if not connected:
            await self._pause("help", f"{site} wants a code. Connect an email under Build Your Profile to enter codes "
                                      "automatically, or type it in the live browser, then continue.")
            return True
        if tries >= MAX_CODE_TRIES or not vault.usable_on(url) or not same_site:
            await self._pause("help", f"{site} still wants a code. Please enter it in the live browser, then continue.")
            return True

        self.code_tries[site] = tries + 1
        since = self.code_since.setdefault(site, datetime.now(timezone.utc) - CODE_LOOKBACK)
        db.add_log(self.id, f"Waiting for the code from {site} at {connected['address']}.")
        try:
            code = await self._wait_for_mail(mailbox.fetch_code, applicant_id, url, since, self.used_codes)
        except mailbox.MailboxError:
            await self._pause("help", f"{connected['address']} refused the saved app password, so the code from {site} "
                                      "could not be read. Please enter it in the live browser, then continue.")
            return True
        if not code or (len(boxes) > 1 and len(code) != len(boxes)):
            await self._pause("help", f"No usable code from {site} arrived at {connected['address']}. Resend it and "
                                      "continue to look again, or enter it in the live browser.")
            return True

        self.used_codes.add(code)
        await forms.enter_code(self.page, boxes, code)
        db.add_log(self.id, f"Entered the code emailed by {site}.")
        if not own_page:
            return False
        await forms.send_code(self.page, boxes)
        await self._settle()
        await self.page.wait_for_timeout(800)
        return True

    async def _wait_for_mail(self, fetch, applicant_id: str, url: str, since: datetime, used: set[str]) -> str | None:
        """Checks the connected mailbox until `fetch` finds something or the wait runs out."""
        deadline = time.monotonic() + config.CODE_WAIT_SECONDS
        while True:
            found = await asyncio.to_thread(fetch, applicant_id, url, since, used)
            if found or time.monotonic() >= deadline:
                return found
            await asyncio.sleep(config.CODE_POLL_SECONDS)

    async def _verify_by_link(self, site: str, applicant_id: str, since: datetime):
        """Opens the verification link the site emailed, then goes back to the job so signing in can continue."""
        url = self.page.url
        connected = mailbox.get(applicant_id)
        if not connected or not vault.usable_on(url):
            await self._pause("help", "The site wants the account's email verified. Finish that in the live browser, then continue.")
            return
        db.add_log(self.id, f"Waiting for the verification email from {site} at {connected['address']}.")
        try:
            link = await self._wait_for_mail(mailbox.fetch_link, applicant_id, url, since, self.used_links)
        except mailbox.MailboxError:
            await self._pause("help", f"{connected['address']} refused the saved app password, so the verification email "
                                      f"from {site} could not be read. Open its link in the live browser, then continue.")
            return
        if not link:
            await self._pause("help", f"No verification link from {site} arrived at {connected['address']}. Open the link "
                                      "from the email in the live browser, then continue.")
            return
        self.used_links.add(link)
        await self.page.goto(link, wait_until="domcontentloaded", timeout=45000)
        await self._settle()
        db.add_log(self.id, f"Opened the verification link emailed by {site}.")
        await self._save_session()
        await self.page.goto(db.get_app(self.id)["job_url"], wait_until="domcontentloaded", timeout=45000)

    async def _sign_in_wall(self, block: str):
        """Use the saved password, offer a new account, or hand over to the person. In that order."""
        if block == "captcha":
            await self._pause("sign_in", _blocker_message(block))
            return
        app = db.get_app(self.id)
        applicant_id = app["applicant_id"]
        profile = db.one("SELECT profile FROM applicants WHERE id=?", applicant_id)["profile"]
        url = self.page.url
        site = vault.site_key(url)

        if site not in self.tried_login and vault.usable_on(url):
            credential = vault.find(applicant_id, site, profile.get("email"))
            if credential:
                self.tried_login.add(site)
                if await self._auto_login(credential, site):
                    return
                if credential["status"] != vault.STATUS_PENDING:
                    await self._pause("sign_in", f"The saved password for {site} did not finish signing in, or the site "
                                                 "wants a code. Please finish in the live browser below.")
                    return
        if (site in self.asked_account or vault.has_any(applicant_id, site)
                or mailbox.registrable_domain(urlparse(url).hostname or "") in NEVER_NEW_ACCOUNT):
            await self._pause("sign_in", _blocker_message(block))
            return

        self.asked_account.add(site)
        if vault.auto_accounts_since(applicant_id):
            db.add_log(self.id, f"{site} needs an account. Creating one, as your automatic accounts setting allows.")
            await self._create_account(site, profile, applicant_id, accept_terms=True)
            return
        await self._pause("account", f"{site} needs an account and none is saved for this applicant. Creating one "
                                     "generates a unique password, saves it encrypted, and accepts the site's terms "
                                     "and privacy notice.")
        if db.get_app(self.id)["decision"] == "create":
            await self._create_account(site, profile, applicant_id)

    async def _auto_login(self, credential: dict, site: str) -> bool:
        host = site.split("#")[0]
        try:
            # Workday opens on its Create Account form, so a saved password would land in a sign-up box.
            if await forms.password_count(self.page) >= 2:
                link = await forms.find_button(self.page, forms.SIGN_IN_LINK)
                if link:
                    await forms.click(self.page, link)
                    await self._settle()
                    await self.page.wait_for_timeout(800)
            filled = await forms.sign_in(self.page, host, credential["login_email"], credential["password"])
        except PlaywrightError:
            log.warning("Automatic sign-in could not fill the form on %s", site)
            filled = False
        if not filled:
            return False
        await self._settle()
        await self.page.wait_for_timeout(1000)
        # Being asked for a code means the password itself was accepted.
        if await forms.blocker(self.page) is None or forms.code_boxes(await forms.scan(self.page)):
            vault.mark(credential["id"], vault.STATUS_ACTIVE)
            await self._save_session()
            db.add_log(self.id, f"Signed in to {site} with the saved password.")
            return True
        if credential["status"] == vault.STATUS_PENDING:
            # Registration never finished, so this password belongs to no account and must not block a new one.
            vault.delete(db.get_app(self.id)["applicant_id"], credential["id"])
            db.add_log(self.id, f"The account on {site} was never finished, so its unused password was removed.")
            return False
        vault.mark(credential["id"], vault.STATUS_FAILED)
        db.add_log(self.id, f"The saved password for {site} did not work.")
        return False

    async def _create_account(self, site: str, profile: dict, applicant_id: str, accept_terms: bool = False):
        email = profile.get("email")
        if not email:
            await self._pause("sign_in", "Add an email to the profile to create accounts. For now, please sign in below.")
            return
        if await forms.password_count(self.page) < 2:
            link = await forms.find_button(self.page, forms.REGISTER_LINK)
            if not link:
                await self._pause("sign_in", f"No way to create an account was found on {site}. Please use the live browser below.")
                return
            await forms.click(self.page, link)
            await self._settle()

        password = vault.generate_password()
        # Saved before submitting, so a crash afterwards can never lose the password.
        vault.save(applicant_id, site, email, password, status=vault.STATUS_PENDING)
        mail_since = datetime.now(timezone.utc) - CODE_LOOKBACK
        for _ in range(5):
            if await forms.blocker(self.page) == "captcha":
                await self._pause("sign_in", _blocker_message("captcha"))
            fields = await forms.scan(self.page)
            boxes = [f for f in fields if f["kind"] == "password"]
            if not boxes:
                break
            if accept_terms:
                for field in fields:
                    if forms.is_terms_box(field) and not field["value"]:
                        await forms.fill(self.page, field, "Yes")
                        db.add_log(self.id, f"Accepted on {site}: \"{field['label'][:300]}\"")
                fields = await forms.scan(self.page)
            questions = await self._fill(fields)
            if questions:
                await self._pause("questions", "Creating the account needs these answers.", questions)
                continue
            for box in boxes:
                await forms.fill(self.page, box, password)
            action = await forms.find_button(self.page, forms.REGISTER_SUBMIT)
            if not action:
                break
            before_url, before_count = self.page.url, len(boxes)
            await forms.click(self.page, action)
            await self._settle()
            await self.page.wait_for_timeout(800)
            if self.page.url != before_url or await forms.password_count(self.page) < before_count:
                db.add_log(self.id, f"Created an account on {site} and saved its password. It shows as working after the first sign-in.")
                await self._save_session()
                if forms.code_boxes(await forms.scan(self.page)):
                    return
                text = await forms.page_text(self.page)
                if re.search(r"verif|confirm your e-?mail|check your (e-?mail|inbox)|enter the code", text, re.I):
                    await self._verify_by_link(site, applicant_id, mail_since)
                return
            errors = await forms.validation_errors(self.page)
            detail = f" It says: {' '.join(errors)[:500]}" if errors else ""
            await self._pause("help", f"The account was not created.{detail} Fix it in the live browser, then continue.")
            return
        await self._pause("help", f"Could not finish creating the account on {site}. Please use the live browser, then continue.")

    async def _fill(self, fields: list[dict]) -> list[dict]:
        uploaded = await self._upload_resume(fields)
        if uploaded:
            # Employers' resume parsers rewrite fields, so read the form again after uploading.
            await self._settle()
            await self.page.wait_for_timeout(800)
            fields = await forms.scan(self.page)

        app = db.get_app(self.id)
        applicant = db.one("SELECT profile FROM applicants WHERE id=?", app["applicant_id"])
        profile, job, answers = applicant["profile"], app["job"], app["answers"]
        saved = db.saved_answers(app["applicant_id"])
        questions: dict[str, dict] = {}
        code_ids = {box["id"] for box in forms.code_boxes(fields)}

        for field in fields:
            if field["kind"] in ("file", "password") or not field["label"] or field["id"] in code_ids:
                continue
            # The site chose these itself, such as a default country, and retyping could only break them.
            if field.get("prompt") and field["value"]:
                continue
            if field.get("listbox") and field["value"]:
                # A resume parser may have picked this, so the profile still wins when it clearly differs.
                known = _known_answer(field, app, profile, saved)
                if known in (None, rules.LEAVE_BLANK) or rules.norm(known) == rules.norm(field["value"]):
                    continue
                field["options"] = await forms.load_options(self.page, field)
                if rules.match_option(known, field["options"]) is None:
                    continue
            if letters.is_cover_field(field["label"]) and field["kind"] in ("text", "textarea"):
                # Written only when mandatory. Otherwise this box stays empty.
                if (field.get("required") or job.get("cover_letter_required")) and not field["value"]:
                    await forms.fill(self.page, field, (await self._cover_letter())["text"])
                continue
            if field.get("listbox") and not field["options"]:
                field["options"] = await forms.load_options(self.page, field)
            key = rules.question_key(field["label"])
            consent = rules.is_consent(field["label"])
            value = _known_answer(field, app, profile, saved)
            if value is None and field.get("required") and not consent and field["kind"] in ("text", "textarea"):
                value = await self._draft(field, job, app, key, answers)

            if value == rules.LEAVE_BLANK:
                if field["value"]:
                    await forms.clear(self.page, field)
                continue
            if value is None:
                known_field = rules.classify(field["label"], field["kind"]) is not None
                if field.get("required") and (not field["value"] or known_field):
                    questions[key] = rules.question_for(field)
                elif field["value"] and known_field:
                    # The saved profile has nothing here, so this came from a resume parser.
                    db.add_log(self.id, f"Cleared a value the employer's parser put in \"{field['label']}\".")
                    await forms.clear(self.page, field)
                continue
            if not await self._enter(field, value) and field.get("required"):
                questions[key] = rules.question_for(field, note=f"The saved answer \"{value}\" is not one of the options.")
        return _numbered(questions)

    async def _enter(self, field: dict, value: str) -> bool:
        kind = field["kind"]
        if kind == "checkbox":
            if (value == "Yes") != bool(field["value"]):
                await forms.fill(self.page, field, value)
            return True
        if kind in ("select", "radio"):
            option = rules.match_option(value, field["options"])
            if option is None:
                return False
            if rules.norm(field["value"]) != rules.norm(option["label"]):
                await forms.fill(self.page, field, option["label"], option)
            return True
        if kind == "checkbox_group":
            # An option such as "No, I don't have migration experience" holds a comma itself, so the
            # whole answer is tried as one option before it is read as a list of several.
            whole = rules.match_option(value, field["options"])
            chosen = [whole] if whole else [rules.match_option(part, field["options"]) for part in value.split(", ")]
            if None in chosen:
                return False
            for option in chosen:
                await forms.fill(self.page, field, option["label"], option)
            return True
        if field["value"] != value:
            await forms.fill(self.page, field, value)
        return True

    async def _resume_file(self) -> dict:
        resume = db.one("SELECT path, filename FROM resumes WHERE id=?", db.get_app(self.id)["resume_id"])
        data = await asyncio.to_thread(storage.store().get, resume["path"])
        mime = "application/pdf" if resume["filename"].lower().endswith(".pdf") else DOCX_MIME
        return {"name": resume["filename"], "mimeType": mime, "buffer": data}

    async def _upload_resume(self, fields: list[dict]) -> bool:
        files = [f for f in fields if f["kind"] == "file"]
        app = db.get_app(self.id)
        letter_required = app["job"].get("cover_letter_required")
        has_letter_box = any(letters.is_cover_field(f["label"]) for f in files)
        done = False
        for field in files:
            label = rules.norm(field["label"])
            if letters.is_cover_field(field["label"]):
                if (field.get("required") or letter_required) and not field["value"] and label not in self.uploaded:
                    letter = await self._cover_letter()
                    await forms.fill(self.page, field, letter["file"])
                    self.uploaded.add(label)
                    self.letter_sent = True
                    db.add_log(self.id, f"Uploaded cover letter {letter['file']['name']}.")
                    done = True
                continue
            is_resume = any(w in label.split() for w in ("resume", "cv")) or "curriculum" in label or len(files) == 1
            if not is_resume:
                if field.get("required"):
                    await self._pause("help", f"\"{field['label']}\" needs a file only you can provide. Attach it in the live browser, then continue.")
                continue
            # Some portals, such as Workday, have one upload box for every document, and carry a
            # resume from their autofill page onto a later step that has the box.
            wants_letter = letter_required and not has_letter_box and not self.letter_sent
            if self.resume_sent or field["value"]:
                if wants_letter and not field["value"]:
                    letter = await self._cover_letter()
                    await forms.fill(self.page, field, letter["file"])
                    self.letter_sent = True
                    db.add_log(self.id, f"Uploaded cover letter {letter['file']['name']}.")
                    done = True
                continue
            upload = await self._resume_file()
            letter = await self._cover_letter() if wants_letter and field.get("multiple") else None
            await forms.fill(self.page, field, [upload, letter["file"]] if letter else upload)
            self.resume_sent = True
            db.add_log(self.id, f"Uploaded resume {upload['name']}.")
            if letter:
                self.letter_sent = True
                db.add_log(self.id, f"Uploaded cover letter {letter['file']['name']}.")
            done = True
        return done

    async def _cover_letter(self) -> dict:
        """This application's letter: one already saved for it, or a new one from the resume and profile."""
        app = db.get_app(self.id)
        job = app["job"]
        saved = job.get("cover_letter")
        if saved:
            data = await asyncio.to_thread(storage.store().get, saved["path"])
        else:
            applicant = db.one("SELECT profile FROM applicants WHERE id=?", app["applicant_id"])
            resume = db.one("SELECT text FROM resumes WHERE id=?", app["resume_id"])
            text = await asyncio.to_thread(letters.write, applicant["profile"], job, resume["text"])
            data = letters.to_docx(text)
            saved = {"path": f"cover_letters/{self.id}/letter.docx", "filename": letters.filename(applicant["profile"]), "text": text}
            await asyncio.to_thread(storage.store().put, saved["path"], data, letters.DOCX_MIME)
            job["cover_letter"] = saved
            db.update_app(self.id, job=job)
            db.add_log(self.id, "Created a cover letter from the resume and profile, because this job requires one.")
        mime = "application/pdf" if saved["filename"].lower().endswith(".pdf") else letters.DOCX_MIME
        return {"text": saved["text"], "file": {"name": saved["filename"], "mimeType": mime, "buffer": data}}

    async def _draft(self, field: dict, job: dict, app: dict, key: str, answers: dict) -> str | None:
        resume = db.one("SELECT text FROM resumes WHERE id=?", app["resume_id"])
        answer = await asyncio.to_thread(ai.draft_answer, field["label"], job, resume["text"], field.get("max_length"))
        if answer:
            answers[key] = answer
            db.update_app(self.id, answers=answers)
            db.add_log(self.id, f"Drafted an answer from the resume for \"{field['label']}\": {answer}")
        return answer

    async def _advance(self, fields: list[dict], action: dict):
        before = forms.signature(self.page.url, fields)
        db.add_log(self.id, f"Clicking \"{action['text']}\".")
        await forms.click(self.page, action)
        await self._settle()
        await self.page.wait_for_timeout(500)
        after = await forms.scan(self.page)
        if forms.signature(self.page.url, after) == before:
            await self.page.wait_for_timeout(2500)  # slow portals may simply not have moved on yet
            after = await forms.scan(self.page)
        if forms.signature(self.page.url, after) == before:
            await self._handle_rejection(after)
        else:
            self.refills = 0

    async def _approve_and_submit(self, fields: list[dict], action: dict):
        app = db.get_app(self.id)
        job = app["job"]
        where = job.get("company") or urlparse(self.page.url).netloc
        if job.get("cover_letter_required") and not self.letter_sent:
            await self._pause("help", "The posting requires a cover letter, but this form never offered a place for it. "
                                      "Attach it in the live browser if you can, then continue.")
            self.letter_sent = True  # the person has now seen this, so it does not block their decision
        declaration = forms.declaration_text(await forms.page_text(self.page))
        reason = f"Ready to submit to {where}. Do you authorize submitting this application?"
        if job.get("cover_letter") and any("Uploaded cover letter" in e["message"] for e in db.get_app(self.id)["log"]):
            reason += f" Cover letter included: {job['cover_letter']['filename']}."
        if declaration:
            reason += f" Submitting accepts this declaration: {declaration}"
        unchecked = [f["label"] for f in fields if f["kind"] == "checkbox" and not f["value"]]
        if unchecked:
            reason += f" Left unchecked: {'; '.join(unchecked)}."
        await self._pause("approval", reason)
        if db.get_app(self.id)["decision"] != "submit":
            db.update_app(self.id, status="SAVED", submission_status="Held by you, not submitted")
            db.add_log(self.id, "Held at the final step. Nothing was submitted.")
            raise _Finished()
        before = forms.signature(self.page.url, fields)
        db.add_log(self.id, f"You approved submission. Clicking \"{action['text']}\".")
        self.submit_clicked = True
        await forms.click(self.page, action)
        await self._settle()
        await self.page.wait_for_timeout(1500)
        after = await forms.scan(self.page)
        controls = await forms.find_actions(self.page)
        if await forms.is_confirmation(self.page) and not controls["submit"] and not controls["next"]:
            await self._confirm()
        if forms.signature(self.page.url, after) == before:
            await self._handle_rejection(after)

    async def _handle_rejection(self, fields: list[dict]):
        """The page refused to move on. Ask about the exact fields it rejected."""
        app = db.get_app(self.id)
        answers = app["answers"]
        where = app["job"].get("company") or "The employer"
        applicant = db.one("SELECT profile FROM applicants WHERE id=?", app["applicant_id"])
        saved = db.saved_answers(app["applicant_id"])
        questions = {}
        refillable = False
        code_ids = {box["id"] for box in forms.code_boxes(fields)}
        for field in fields:
            if field["kind"] in ("file", "password") or not field["label"] or field["id"] in code_ids:
                continue
            blank = not field["value"]
            if field.get("invalid") or (field.get("required") and blank):
                key = rules.question_key(field["label"])
                # Many sites reload the step empty after an error. What we already know goes back in.
                known = _known_answer(field, app, applicant["profile"], saved)
                if blank and known not in (None, rules.LEAVE_BLANK) and self.refills < MAX_REFILLS:
                    refillable = True
                    continue
                if field.get("listbox"):
                    field["options"] = await forms.load_options(self.page, field)
                note = (f"{where} requires this field and rejected it when blank." if blank
                        else f"{where} rejected the value \"{field['value']}\".")
                questions[key] = rules.question_for(field, note=note)
                answers.pop(key, None)
                db.execute("DELETE FROM saved_answers WHERE applicant_id=? AND key=?", app["applicant_id"], key)
        if questions:
            db.update_app(self.id, answers=answers)
            await self._pause("questions", f"{where} would not accept this step.", _numbered(questions))
            return
        if refillable:
            self.refills += 1
            return
        errors = await forms.validation_errors(self.page)
        detail = f" It says: {' '.join(errors)[:500]}" if errors else ""
        await self._pause("help", f"The page did not move on and is not confirmed as submitted.{detail} Check it in the live browser, then continue.")

    async def _confirm(self):
        shot = await self._snapshot()
        await self._save_session()
        db.update_app(self.id, status="SUBMITTED", progress=100, submission_status="Confirmed by the employer's page",
                      screenshot=shot, pause_kind="", pause_reason="", questions=[], current_page=self.page.url)
        db.add_log(self.id, "Submission confirmed by the employer's page. Moved to Applied.")
        raise _Finished()


def _known_answer(field: dict, app: dict, profile: dict, saved: dict) -> str | None:
    """This application's answers first, then the profile, then earlier answers. Consent is never reused."""
    key = rules.question_key(field["label"])
    value = app["answers"].get(key)
    if value is None and not rules.is_consent(field["label"]):
        value = rules.resolve(field, profile, app["job"], app["job_url"])
        if value is None:
            value = saved.get(key)
    return value


def _looks_like_form(fields: list[dict], actions: dict) -> bool:
    """Careful here: mistaking a final form for a job page would click its Apply button unapproved."""
    if actions["next"] and fields:
        return True
    if any(f["kind"] == "file" for f in fields):
        return True
    if len(fields) >= 2 and any(f.get("required") for f in fields):
        return True
    return bool(fields and actions["submit"] and actions["submit"] is not actions["apply"]
                and not forms._APPLY.search(actions["submit"]["text"]))


def _numbered(questions: dict[str, dict]) -> list[dict]:
    return [{**q, "number": i} for i, q in enumerate(questions.values(), start=1)]


def _blocker_message(kind: str) -> str:
    if kind == "captcha":
        return "This site is showing a CAPTCHA. Please complete it in the live browser."
    return "Sign-in is needed. Please sign in using the live browser below. Your password is never stored."


def _load_session(applicant_id: str) -> dict | None:
    row = db.one("SELECT browser_session FROM applicants WHERE id=?", applicant_id)
    if not row or not row["browser_session"]:
        return None
    try:
        return json.loads(config.FERNET.decrypt(bytes(row["browser_session"])))
    except InvalidToken:
        log.warning("Could not decrypt the saved browser session for %s", applicant_id)
        return None


class Runner:
    """Owns the Playwright loop on its own thread, separate from the web server's."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.jobs: dict[str, Job] = {}
        self.slots: asyncio.Semaphore | None = None
        self._playwright = None
        self._browser = None
        self._ready = threading.Event()

    def start(self):
        threading.Thread(target=self._main, daemon=True, name="browser-worker").start()
        self._ready.wait()

    def _main(self):
        self.loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.slots = asyncio.Semaphore(config.MAX_LIVE_BROWSERS)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def stop(self):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(timeout=20)
            self.loop.call_soon_threadsafe(self.loop.stop)

    async def _shutdown(self):
        for job in list(self.jobs.values()):
            await job._close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def browser(self):
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = await self._playwright.chromium.launch(headless=config.HEADLESS)
        return self._browser

    def is_live(self, app_id: str) -> bool:
        return app_id in self.jobs

    def enqueue(self, app_id: str):
        self.loop.call_soon_threadsafe(self._start, app_id)

    def _start(self, app_id: str):
        if app_id not in self.jobs:
            job = Job(self, app_id)
            self.jobs[app_id] = job
            self.loop.create_task(job.run())

    def resume(self, app_id: str):
        """Wake the job waiting on this application, or start it again if its browser was released."""
        def wake():
            job = self.jobs.get(app_id)
            if job:
                job.wake.set()
            else:
                self._start(app_id)
        self.loop.call_soon_threadsafe(wake)

    def live_frame(self, app_id: str) -> bytes | None:
        job = self.jobs.get(app_id)
        if not job or not job.context:
            return None
        future = asyncio.run_coroutine_threadsafe(job.page.screenshot(type="jpeg", quality=60), self.loop)
        try:
            return future.result(timeout=10)
        except (PlaywrightError, TimeoutError):
            return None

    def live_input(self, app_id: str, event: dict) -> bool:
        """Forward the person's click or key. Only while paused, and the content is never logged."""
        job = self.jobs.get(app_id)
        if not job or not job.paused:
            return False
        job.last_input = time.monotonic()
        asyncio.run_coroutine_threadsafe(_send_input(job.page, event), self.loop).result(timeout=10)
        return True


async def _send_input(page, event: dict):
    kind = event.get("type")
    if kind == "click":
        await page.mouse.click(float(event["x"]), float(event["y"]))
    elif kind == "text":
        await page.keyboard.type(str(event["text"]))
    elif kind == "key":
        await page.keyboard.press(str(event["key"]))
    elif kind == "scroll":
        await page.mouse.wheel(0, float(event["dy"]))


runner = Runner()
