"""A local stand-in for the employer flow in the Croda chat. All data here is made up.

Job page -> Apply needs sign-in -> Step 1 (rejects a blank preferred name, salary must be a
number) -> Step 2 (resume parser puts a state into City, demographics required) -> Step 3
(declaration, optional opt-in box) -> "Thank you".
"""
import html
from datetime import datetime, timezone
from email.utils import format_datetime

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

site = FastAPI()
state = {"submissions": [], "step1": {}, "step2": {}, "submit_clicks": 0}


def reset():
    state.update(submissions=[], step1={}, step2={}, submit_clicks=0)
    portal["users"].clear()
    otp.update(code="482913", emailed=None, send_email=True, accepted=[], rejected=[])
    inbox.clear()
    linkco.update(verified=set(), send_email=True)


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><title>{title}</title><body><header><input name=q aria-label='Search jobs'></header>{body}</body>")


@site.get("/jobs/1")
def job():
    return page(
        "Fixture Labs hiring Site IT Engineer in Dover, DE | LinkedIn",
        "<h1>Site IT Engineer</h1><p>Fixture Labs, Dover, DE. Salary: $140,000 - $150,000.</p>"
        "<a href='/apply/step1' role='button'>Apply</a>",
    )


@site.get("/login")
def login_page(next: str = "/apply/step1"):
    return page("Sign in", f"<h1>Sign in</h1><form method=post action='/login?next={next}'>"
                "<label>Email <input name=username></label>"
                "<label>Password <input type=password name=password></label>"
                "<button>Sign in</button></form>")


@site.post("/login")
async def login(request: Request, next: str = "/apply/step1"):
    response = RedirectResponse(next, status_code=303)
    response.set_cookie("fixture_session", "ok")
    return response


def _gate(request: Request):
    return None if request.cookies.get("fixture_session") else RedirectResponse("/login", status_code=303)


@site.get("/apply/step1")
def step1(request: Request, error: str = ""):
    if gate := _gate(request):
        return gate
    invalid = "class='error'" if error else ""
    message = "<div role='alert'>Preferred name is required</div>" if error == "preferred" else ""
    salary_msg = "<div role='alert'>Salary must be a number</div>" if error == "salary" else ""
    return page("Step 1 of 3", f"""
    <h2>Step 1 of 3</h2>{message}{salary_msg}
    <form method=post action='/apply/step1'>
      <label>First name * <input name=first_name required></label>
      <label>Surname * <input name=surname required></label>
      <div {invalid if error == 'preferred' else ''}><label>Preferred name * <input name=preferred aria-invalid="{'true' if error == 'preferred' else 'false'}"></label></div>
      <label>Email * <input type=email name=email required></label>
      <label>Retype email address * <input type=email name=email2 required></label>
      <label>Phone * <input name=phone required></label>
      <fieldset><legend>Will you need US visa sponsorship now or in the future? *</legend>
        <label><input type=radio name=sponsor value=yes required>Yes</label>
        <label><input type=radio name=sponsor value=no>No</label></fieldset>
      <fieldset><legend>Any relatives employed by Fixture Labs? *</legend>
        <label><input type=radio name=relatives value=yes required>Yes</label>
        <label><input type=radio name=relatives value=no>No</label></fieldset>
      <fieldset><legend>Are you subject to a non-compete agreement affecting this role? *</legend>
        <label><input type=radio name=noncompete value=yes required>Yes</label>
        <label><input type=radio name=noncompete value=no>No</label></fieldset>
      <label>Salary expectation * <input name=salary required></label>
      <label>Notice period <input name=notice></label>
      <label>Bonus expectation <input name=bonus></label>
      <label>How did you hear about us? *
        <select name=source required><option value=''>Select</option><option>LinkedIn</option><option>Indeed</option></select></label>
      <button>Next</button>
    </form>""")


@site.post("/apply/step1")
async def step1_post(request: Request):
    form = dict(await request.form())
    if not form.get("preferred"):
        return RedirectResponse("/apply/step1?error=preferred", status_code=303)
    if not str(form.get("salary", "")).isdigit():
        return RedirectResponse("/apply/step1?error=salary", status_code=303)
    state["step1"] = form
    return RedirectResponse("/apply/step2", status_code=303)


@site.get("/apply/step2")
def step2(request: Request):
    if gate := _gate(request):
        return gate
    return page("Step 2 of 3", """
    <h2>Step 2 of 3</h2>
    <form method=post action='/apply/step2' enctype='multipart/form-data'>
      <label>Upload resume * <input type=file name=resume required></label>
      <label>City <input name=city id=city></label>
      <label>Are you willing to relocate? <input name=relocate></label>
      <fieldset><legend>Gender *</legend>
        <label><input type=radio name=gender value=m required>Male</label>
        <label><input type=radio name=gender value=f>Female</label>
        <label><input type=radio name=gender value=x>Do not wish to disclose</label></fieldset>
      <label>Race *
        <select name=race required><option value=''>Select</option><option>Asian</option><option>White</option><option>Do not wish to disclose</option></select></label>
      <label>Veteran status *
        <select name=veteran required><option value=''>Select</option><option>None of the above</option><option>Disabled Veteran</option><option>Do not wish to disclose</option></select></label>
      <button>Next</button>
    </form>
    <script>
      // The employer's resume parser writing a wrong value, as Croda's did.
      document.querySelector('input[type=file]').addEventListener('change', () => {
        document.getElementById('city').value = 'Delaware';
      });
    </script>""")


@site.post("/apply/step2")
async def step2_post(request: Request):
    form = await request.form()
    data = {k: v for k, v in form.items() if not hasattr(v, "filename")}
    upload = form.get("resume")
    data["resume_name"] = upload.filename if upload is not None else ""
    data["resume_size"] = len(await upload.read()) if upload is not None else 0
    state["step2"] = data
    return RedirectResponse("/apply/step3", status_code=303)


@site.get("/apply/step3")
def step3(request: Request):
    if gate := _gate(request):
        return gate
    return page("Step 3 of 3", """
    <h2>Step 3 of 3</h2>
    <p>I declare that the information given is true and accurate. False information may lead to a withdrawn offer.</p>
    <form method=post action='/apply/step3'>
      <label><input type=checkbox name=searchable value=1>Consider me for other positions and make my information searchable</label>
      <button>Submit</button>
    </form>""")


@site.post("/apply/step3")
async def step3_post(request: Request):
    form = dict(await request.form())
    state["submit_clicks"] += 1
    state["submissions"].append({**state["step1"], **state["step2"], **form})
    return RedirectResponse("/apply/done", status_code=303)


@site.get("/apply/done")
def done():
    return page("Thank you", "<h1>Thank you</h1><p>Your application has been received.</p>")


# A second employer whose portal requires its own account, as Vicor's did.

portal = {"users": {}}


@site.get("/portal/jobs/2")
def portal_job():
    return page("Portal Co hiring Network Engineer in Andover, MA", "<h1>Network Engineer</h1><p>Portal Co</p>"
                "<a href='/portal/apply' role='button'>Apply</a>")


@site.get("/portal/login")
def portal_login_page(error: str = ""):
    message = "<div role='alert'>Wrong email or password</div>" if error else ""
    return page("Portal sign in", f"<h1>Candidate sign in</h1>{message}<form method=post action='/portal/login'>"
                "<label>Email <input type=email name=email></label>"
                "<label>Password <input type=password name=password></label><button>Sign in</button></form>"
                "<a href='/portal/register'>Create an account</a>")


@site.post("/portal/login")
async def portal_login(request: Request):
    form = dict(await request.form())
    if portal["users"].get(form.get("email")) != form.get("password"):
        return RedirectResponse("/portal/login?error=1", status_code=303)
    response = RedirectResponse("/portal/apply", status_code=303)
    response.set_cookie("portal_session", form["email"])
    return response


@site.get("/portal/register")
def portal_register_page(error: str = ""):
    message = "<div role='alert'>Passwords must be 8-18 characters and match</div>" if error else ""
    return page("Create account", f"""<h1>Create account</h1>{message}
    <form method=post action='/portal/register'>
      <label>First name * <input name=first required></label>
      <label>Last name * <input name=last required></label>
      <label>Email * <input type=email name=email required></label>
      <label>Retype email address * <input type=email name=email2 required></label>
      <label>Choose password * <input type=password name=p1 required></label>
      <label>Retype password * <input type=password name=p2 required></label>
      <label><input type=checkbox name=terms value=1 required>I agree to the data privacy terms *</label>
      <label><input type=checkbox name=alerts value=1>Send me marketing emails and job alerts</label>
      <button>Create Account</button>
    </form>""")


@site.post("/portal/register")
async def portal_register(request: Request):
    form = dict(await request.form())
    if form.get("p1") != form.get("p2") or not 8 <= len(form.get("p1", "")) <= 18 or " " in form["p1"]:
        return RedirectResponse("/portal/register?error=1", status_code=303)
    portal["users"][form["email"]] = form["p1"]
    portal["registration"] = form
    response = RedirectResponse("/portal/apply", status_code=303)
    response.set_cookie("portal_session", form["email"])
    return response


@site.get("/portal/apply")
def portal_apply(request: Request):
    if not request.cookies.get("portal_session"):
        return RedirectResponse("/portal/login", status_code=303)
    return page("Apply", "<h2>Application</h2><form method=post action='/portal/apply'>"
                "<label>First name * <input name=first required></label>"
                "<label>Email * <input type=email name=email required></label>"
                "<button>Submit</button></form>")


@site.post("/portal/apply")
async def portal_apply_post(request: Request):
    state["submissions"].append(dict(await request.form()))
    state["submit_clicks"] += 1
    return RedirectResponse("/apply/done", status_code=303)


# A third employer that emails a code after the password, on a page under /login.

otp = {"code": "482913", "emailed": None, "send_email": True, "accepted": [], "rejected": []}
inbox: list[bytes] = []


def code_email(code: str, sender: str, sent=None) -> bytes:
    when = format_datetime(sent or datetime.now(timezone.utc))
    return (f"From: Careers <{sender}>\r\nTo: test.person@example.test\r\nDate: {when}\r\n"
            f"Subject: Verify your sign-in\r\nContent-Type: text/plain\r\n\r\n"
            f"Your verification code is {code}. It expires in 10 minutes.\r\n").encode()


@site.get("/otp/jobs/3")
def otp_job():
    return page("Code Co hiring Analyst in Austin, TX", "<h1>Analyst</h1><p>Code Co</p>"
                "<a href='/otp/apply' role='button'>Apply</a>")


@site.get("/otp/login")
def otp_login_page():
    return page("Sign in", "<h1>Sign in</h1><form method=post action='/otp/login'>"
                "<label>Email <input type=email name=email></label>"
                "<label>Password <input type=password name=password></label><button>Sign in</button></form>")


@site.post("/otp/login")
async def otp_login(request: Request):
    form = dict(await request.form())
    if portal["users"].get(form.get("email")) != form.get("password"):
        return RedirectResponse("/otp/login", status_code=303)
    if otp["send_email"]:
        inbox.append(code_email(otp["emailed"] or otp["code"], "no-reply@127.0.0.1"))
    return RedirectResponse("/otp/login/verify", status_code=303)


@site.get("/otp/login/verify")
def otp_verify_page(error: str = ""):
    message = "<div role='alert'>That code is not right</div>" if error else ""
    return page("Enter code", f"<h1>Check your email</h1>{message}<form method=post action='/otp/login/verify'>"
                "<label>Verification code <input name=code autocomplete=one-time-code></label>"
                "<button>Verify</button></form>")


@site.post("/otp/login/verify")
async def otp_verify(request: Request):
    code = dict(await request.form()).get("code", "")
    if code != otp["code"]:
        otp["rejected"].append(code)
        return RedirectResponse("/otp/login/verify?error=1", status_code=303)
    otp["accepted"].append(code)
    response = RedirectResponse("/otp/apply", status_code=303)
    response.set_cookie("otp_session", "ok")
    return response


@site.get("/otp/apply")
def otp_apply(request: Request):
    if not request.cookies.get("otp_session"):
        return RedirectResponse("/otp/login", status_code=303)
    return page("Apply", "<h2>Application</h2><form method=post action='/otp/apply'>"
                "<label>First name * <input name=first required></label>"
                "<label>Email * <input type=email name=email required></label>"
                "<button>Submit</button></form>")


@site.post("/otp/apply")
async def otp_apply_post(request: Request):
    state["submissions"].append(dict(await request.form()))
    state["submit_clicks"] += 1
    return RedirectResponse("/apply/done", status_code=303)


# A single form with the code box beside the application, whose only button submits the application.

@site.get("/otp/final/jobs/4")
def otp_final_job():
    inbox.append(code_email(otp["code"], "no-reply@127.0.0.1"))
    return page("Final Co hiring Analyst", "<h1>Analyst</h1><p>Final Co</p><a href='/otp/final' role='button'>Apply</a>")


@site.get("/otp/final")
def otp_final():
    return page("Apply", "<h2>Application</h2><form method=post action='/otp/final'>"
                "<label>First name * <input name=first required></label>"
                "<label>Email * <input type=email name=email required></label>"
                "<label>Security code * <input name=code required></label>"
                "<button>Submit</button></form>")


@site.post("/otp/final")
async def otp_final_post(request: Request):
    state["submissions"].append(dict(await request.form()))
    state["submit_clicks"] += 1
    return RedirectResponse("/apply/done", status_code=303)


# A job board whose sign-in popup covers the Apply button until the person signs in.

@site.get("/board/jobs/5")
def board_job(request: Request):
    popup = "" if request.cookies.get("board_session") else (
        "<div role='dialog' style='position:fixed;inset:0;background:#fff'><p>Join or sign in to find your next job</p>"
        "<button onclick=\"document.cookie='board_session=ok';location.reload()\">Sign in</button></div>")
    return page("Board Co hiring Analyst", f"<h1>Analyst</h1><p>Board Co</p><a href='/apply/done' role='button'>Apply</a>{popup}")


# Apply opens a popup offering the site's own autofill or a manual form, as Alterra's careers site does.

@site.get("/popup/jobs/6")
def popup_job():
    return page("Popup Co hiring Analyst", """<h1>Analyst</h1><p>Popup Co</p>
    <button onclick="document.getElementById('choice').showModal()">Apply now</button>
    <dialog id=choice><a href='/popup/autofill'>Autofill with resume</a> <a href='/popup/form'>Apply manually</a></dialog>""")


@site.get("/popup/autofill")
def popup_autofill():
    return page("Autofill", "<h2>Upload your resume</h2><form method=post action='/popup/autofill' enctype='multipart/form-data'>"
                "<label>Resume <input type=file name=resume required></label><button>Continue</button></form>")


@site.post("/popup/autofill")
async def popup_autofill_post(request: Request):
    state["step2"] = {"autofill": (await request.form())["resume"].filename}
    return RedirectResponse("/popup/form", status_code=303)


@site.get("/popup/form")
def popup_form():
    # The parser's guess is wrong on purpose. The one upload box takes any further documents.
    return page("Apply", "<h2>Application</h2><form method=post action='/popup/send' enctype='multipart/form-data'>"
                "<label>First name * <input name=first required value='Parsed Wrong'></label>"
                "<label>Email * <input type=email name=email required></label>"
                "<label>Upload a file <input type=file name=documents multiple></label>"
                "<button>Submit</button></form>")


@site.post("/popup/send")
async def popup_send(request: Request):
    form = await request.form()
    state["submit_clicks"] += 1
    state["submissions"].append({"first": form.get("first"), "documents": [f.filename for f in form.getlist("documents") if f.filename]})
    return RedirectResponse("/apply/done", status_code=303)


@site.get("/popup/letter/jobs/11")
def popup_letter_job():
    return page("Popup Co hiring Analyst", """<h1>Analyst</h1><p>Resume and cover letter are required.</p>
    <button onclick="document.getElementById('choice').showModal()">Apply now</button>
    <dialog id=choice><a href='/popup/form?m=1'>Apply manually</a> <a href='/popup/autofill'>Autofill with resume</a></dialog>""")


# Workday's widgets, on a form drawn late: dropdowns whose options exist only when open, and a
# search box that already holds a chosen value.

@site.get("/workday/jobs/7")
def workday_job():
    return page("Workday Co hiring Analyst", "<h1>Analyst</h1><p>Workday Co</p><a href='/workday/form' role='button'>Apply</a>")


@site.get("/workday/form")
def workday_form():
    return page("My Information", """<div id=root></div><script>
    setTimeout(() => {
      root.innerHTML = `<h2>My Information</h2><form method=post action='/workday/form'>
        <input type=hidden name=state id=state><input type=hidden name=device id=device>
        <label for=stateBox>State*</label>
        <button type=button id=stateBox name=stateBox aria-haspopup=listbox aria-label="State Select One Required">Select One</button>
        <label for=deviceBox>Phone Device Type*</label>
        <button type=button id=deviceBox name=deviceBox aria-haspopup=listbox>Select One</button>
        <label for=staffBox></label>
        <button type=button id=staffBox aria-haspopup=listbox aria-label="Are you a current employee? Select One Required">Select One</button>
        <label for=suffixBox>Suffix</label>
        <button type=button id=suffixBox name=suffixBox aria-haspopup=listbox>Select One</button>
        <label for=countryBox>Country*</label>
        <button type=button id=countryBox name=countryBox aria-haspopup=listbox>United States of America</button>
        <div data-automation-id=multiSelectContainer>
          <label for=code>Country Phone Code*</label>
          <input id=code aria-required=true data-uxi-widget-type=selectinput>
          <ul role=listbox data-automation-id=selectedItemList><li role=option>United States of America (+1)</li></ul>
        </div>
        <label>First Name* <input name=first aria-required=true></label>
        <button type=button id=next onclick="this.form.submit()">Next</button>
      </form><div id=pop></div>`;
      const lists = {stateBox: ['Select One', 'Colorado', 'Illinois'], deviceBox: ['Mobile', 'Landline'], staffBox: ['Yes', 'No'],
                     suffixBox: ['Jr.', 'Sr.'], countryBox: ['Canada', 'United States of America']};
      const fields = {stateBox: 'state', deviceBox: 'device'};
      for (const [id, options] of Object.entries(lists)) {
        document.getElementById(id).onclick = e => {
          pop.innerHTML = '<ul role=listbox>' + options.map(o => '<li role=option>' + o + '</li>').join('') + '</ul>';
          pop.querySelectorAll('li').forEach(li => li.onclick = () => {
            e.target.textContent = li.textContent;
            if (fields[id]) document.getElementById(fields[id]).value = li.textContent;
            pop.innerHTML = '';
          });
        };
      }
      document.addEventListener('keydown', k => { if (k.key === 'Escape') pop.innerHTML = ''; });
    }, 3500);
    </script>""")


@site.post("/workday/form")
async def workday_form_post(request: Request):
    state["step1"] = dict(await request.form())
    return RedirectResponse("/workday/review", status_code=303)


@site.get("/workday/review")
def workday_review():
    return page("Review", "<h2>Review</h2><form method=post action='/apply/step3'><button>Submit</button></form>")


# Cover letters: one posting demands one and has a single upload box, another has an optional box
# and text area, and a third says nothing but its form requires one.

def _letter_form(boxes: str) -> str:
    return (f"<h2>Application</h2><form method=post action='/letter/send' enctype='multipart/form-data'>"
            f"<label>First name * <input name=first required></label>{boxes}<button>Next</button></form>")


@site.get("/letter/required/jobs/8")
def letter_required_job():
    return page("Letter Co hiring Analyst", "<h1>Analyst</h1><p>ATTN: To be considered, resume and cover letter are required.</p>"
                "<a href='/letter/required/form' role='button'>Apply</a>")


@site.get("/letter/required/form")
def letter_required_form():
    return page("Apply", _letter_form("<label>Upload a file <input type=file name=documents multiple></label>"))


@site.get("/letter/optional/jobs/9")
def letter_optional_job():
    return page("Letter Co hiring Analyst", "<h1>Analyst</h1><p>A cover letter is optional.</p>"
                "<a href='/letter/optional/form' role='button'>Apply</a>")


@site.get("/letter/optional/form")
def letter_optional_form():
    return page("Apply", _letter_form("<label>Resume <input type=file name=documents></label>"
                                      "<label>Cover letter <input type=file name=cover></label>"
                                      "<label>Cover letter text <textarea name=cover_text></textarea></label>"))


@site.get("/letter/form-only/jobs/10")
def letter_form_only_job():
    return page("Letter Co hiring Analyst", "<h1>Analyst</h1><a href='/letter/form-only/form' role='button'>Apply</a>")


@site.get("/letter/form-only/form")
def letter_form_only_form():
    return page("Apply", _letter_form("<label>Resume <input type=file name=documents></label>"
                                      "<label>Cover letter * <input type=file name=cover required></label>"))


@site.post("/letter/send")
async def letter_send(request: Request):
    form = await request.form()
    state["step1"] = {
        "documents": [f.filename for f in form.getlist("documents") if f.filename],
        "cover": getattr(form.get("cover"), "filename", ""),
        "cover_text": form.get("cover_text", ""),
    }
    return RedirectResponse("/workday/review", status_code=303)


# After submitting, thanks and an offer of an account share one page, as on Workday.

@site.get("/thanks/jobs/12")
def thanks_job():
    return page("Thanks Co hiring Analyst", "<h1>Analyst</h1><a href='/thanks/form' role='button'>Apply</a>")


@site.get("/thanks/form")
def thanks_form():
    return page("Apply", "<h2>Application</h2><form method=post action='/thanks/form'>"
                "<label>First name * <input name=first required></label>"
                "<label>Email * <input type=email name=email required></label><button class=g-recaptcha>Submit</button></form>"
                # Greenhouse's invisible reCAPTCHA badge, which never asks anything.
                "<div class=grecaptcha-badge style='position:fixed;bottom:0;right:0;width:256px;height:60px'>"
                "<div class=grecaptcha-logo>reCAPTCHA</div></div>")


@site.post("/thanks/form")
async def thanks_form_post():
    state["submit_clicks"] += 1
    return RedirectResponse("/thanks/done", status_code=303)


@site.get("/thanks/done")
def thanks_done():
    return page("Careers", "<div role=dialog><h1>Congratulations!</h1><p>Thank you for applying to Thanks Co!</p>"
                "<label>Password * <input type=password name=password></label><button>Create Account</button></div>")


@site.get("/nothing")
def nothing():
    return page("Nothing", "<p>" + html.escape("This position is closed.") + "</p>")


# An employer that emails a verification link after sign-up and refuses sign-in until it is opened.

linkco = {"verified": set(), "token": "tok-7f3a91"}


def link_email(link: str, sender: str) -> bytes:
    when = format_datetime(datetime.now(timezone.utc))
    return (f"From: Careers <{sender}>\r\nTo: test.person@example.test\r\nDate: {when}\r\n"
            f"Subject: Confirm your email\r\nContent-Type: text/html\r\n\r\n"
            f"<p>Thanks for joining.</p><p><a href=\"{link}\">Verify my email address</a></p>"
            f"<p><a href=\"{link.split('/link/')[0]}/link/unsubscribe\">Unsubscribe</a></p>\r\n").encode()


@site.get("/link/jobs/7")
def link_job():
    return page("Link Co hiring Support Engineer in Denver, CO", "<h1>Support Engineer</h1><p>Link Co</p>"
                "<a href='/link/apply' role='button'>Apply</a>")


@site.get("/link/login")
def link_login_page(error: str = ""):
    message = "<div role='alert'>Please verify your email before signing in</div>" if error else ""
    return page("Link sign in", f"<h1>Candidate sign in</h1>{message}<form method=post action='/link/login'>"
                "<label>Email <input type=email name=email></label>"
                "<label>Password <input type=password name=password></label><button>Sign in</button></form>"
                "<a href='/link/register'>Create an account</a>")


@site.post("/link/login")
async def link_login(request: Request):
    form = dict(await request.form())
    if portal["users"].get(form.get("email")) != form.get("password") or form.get("email") not in linkco["verified"]:
        return RedirectResponse("/link/login?error=1", status_code=303)
    response = RedirectResponse("/link/apply", status_code=303)
    response.set_cookie("link_session", form["email"])
    return response


@site.get("/link/register")
def link_register_page():
    return page("Create account", """<h1>Create account</h1>
    <form method=post action='/link/register'>
      <label>First name * <input name=first required></label>
      <label>Last name * <input name=last required></label>
      <label>Email * <input type=email name=email required></label>
      <label>Choose password * <input type=password name=p1 required></label>
      <label>Retype password * <input type=password name=p2 required></label>
      <label><input type=checkbox name=terms value=1 required>I agree to the data privacy terms *</label>
      <button>Create Account</button>
    </form>""")


@site.post("/link/register")
async def link_register(request: Request):
    form = dict(await request.form())
    if form.get("p1") != form.get("p2"):
        return RedirectResponse("/link/register", status_code=303)
    portal["users"][form["email"]] = form["p1"]
    if linkco.get("send_email", True):
        base = str(request.base_url).rstrip("/")
        inbox.append(link_email(f"{base}/link/verify?token={linkco['token']}&email={form['email']}", "no-reply@127.0.0.1"))
    return RedirectResponse("/link/check-email", status_code=303)


@site.get("/link/check-email")
def link_check_email():
    return page("Check your email", "<h1>Almost done</h1><p>Check your inbox and click the link to verify your email address.</p>")


@site.get("/link/verify")
def link_verify(token: str = "", email: str = ""):
    if token != linkco["token"]:
        return page("Link expired", "<p>That link is not valid.</p>")
    linkco["verified"].add(email)
    return page("Email verified", "<h1>Email verified</h1><p>You can now sign in.</p><a href='/link/login'>Sign in</a>")


@site.get("/link/apply")
def link_apply(request: Request):
    if not request.cookies.get("link_session"):
        return RedirectResponse("/link/login", status_code=303)
    return page("Apply", "<h2>Application</h2><form method=post action='/link/apply'>"
                "<label>First name * <input name=first required></label>"
                "<label>Email * <input type=email name=email required></label>"
                "<button>Submit</button></form>")


@site.post("/link/apply")
async def link_apply_post(request: Request):
    state["submissions"].append(dict(await request.form()))
    state["submit_clicks"] += 1
    return RedirectResponse("/apply/done", status_code=303)


# An "Upload Resume" button that opens the system file picker from a box made on the spot, as Paylocity's does.

@site.get("/picker/jobs/8")
def picker_job():
    return page("Picker Co hiring Analyst", """<h1>Analyst</h1>
    <form method=post action='/picker/send' id=f>
      <label>Favorite tool * <input name=tool required></label>
      <input type=hidden name=uploaded id=uploaded>
      <button>Submit</button>
    </form>
    <button type=button style='position:fixed;top:0;left:0;width:240px;height:60px' onclick="
      const box = document.createElement('input'); box.type = 'file';
      box.onchange = () => { document.getElementById('uploaded').value = box.files[0].name; };
      box.click();">Upload Resume</button>""")


@site.post("/picker/send")
async def picker_send(request: Request):
    state["submissions"].append(dict(await request.form()))
    state["submit_clicks"] += 1
    return RedirectResponse("/apply/done", status_code=303)


# A required checkbox group whose options hold commas, as Greenhouse screening questions do.

@site.get("/group/jobs/9")
def group_job():
    return page("Group Co hiring Analyst", """<h1>Analyst</h1>
    <form method=post action='/group/send'>
      <label>Email * <input type=email name=email required></label>
      <fieldset><legend>Have you migrated users between SaaS platforms? *</legend>
        <label><input type=checkbox name=mig value=led>Yes, I led one or more migrations</label>
        <label><input type=checkbox name=mig value=none>No, I don't have migration experience</label>
      </fieldset>
      <button>Submit</button>
    </form>""")


@site.post("/group/send")
async def group_send(request: Request):
    form = await request.form()
    state["submissions"].append({"mig": form.getlist("mig")})
    state["submit_clicks"] += 1
    return RedirectResponse("/apply/done", status_code=303)


# Workday opens on Create Account, and its email box is a plain text box named only by aria-label.

@site.get("/wd/jobs/10")
def wd_job():
    return page("WD Co hiring Analyst", "<h1>Analyst</h1><a href='/wd/apply' role='button'>Apply</a>")


@site.get("/wd/apply")
def wd_apply(request: Request):
    if not request.cookies.get("wd_session"):
        return RedirectResponse("/wd/register", status_code=303)
    return page("Apply", "<h2>Application</h2><form method=post action='/wd/apply'>"
                "<label>First name * <input name=first required></label><button>Submit</button></form>")


@site.get("/wd/register")
def wd_register():
    return page("Create Account", """<h1>Create Account</h1><form method=post action='/wd/register'>
      <input type=text data-automation-id=email aria-label='Email Address'>
      <input type=password aria-label=Password><input type=password aria-label='Verify New Password'>
      <button>Create Account</button></form>
      <p>Already have an account? <button type=button onclick="document.getElementById('signin').showModal()">Sign In</button></p>
      <dialog id=signin><h2>Sign In</h2><form method=post action='/wd/login'>
        <input type=text name=a1 data-automation-id=email aria-label='Email Address'>
        <input type=password name=a2 aria-label=Password><button>Sign In</button></form></dialog>""")


@site.get("/wd/login")
def wd_login_page():
    return page("Sign In", """<h1>Sign In</h1><form method=post action='/wd/login'>
      <input type=text name=a1 data-automation-id=email aria-label='Email Address'>
      <input type=password name=a2 aria-label=Password><button>Sign In</button></form>""")


@site.post("/wd/login")
async def wd_login(request: Request):
    form = dict(await request.form())
    if portal["users"].get(form.get("a1")) != form.get("a2"):
        return RedirectResponse("/wd/login", status_code=303)
    response = RedirectResponse("/wd/apply", status_code=303)
    response.set_cookie("wd_session", "ok")
    return response


@site.post("/wd/apply")
async def wd_apply_post(request: Request):
    state["submissions"].append(dict(await request.form()))
    state["submit_clicks"] += 1
    return RedirectResponse("/apply/done", status_code=303)
