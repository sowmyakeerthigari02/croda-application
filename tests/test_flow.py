"""Runs the chat's whole journey through the API and a real browser against the local fixture."""
import io
import time

import pytest
from pypdf import PdfWriter

from tests.fixture_site import state


def test_an_apply_popup_uses_the_resume_choice_and_the_profile_still_wins(client, fixture_url):
    applicant = make_applicant(client)
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/popup/jobs/6"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app_id = response.json()["id"]
    app = wait_for(client, app_id, paused("approval"))
    assert app["current_page"].endswith("/popup/form")
    assert state["step2"]["autofill"] == "Test_Resume.pdf"
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"})
    wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED")
    assert state["submissions"][-1]["first"] == "Test", "the profile replaces the parser's guess"
    assert state["submissions"][-1]["documents"] == [], "the resume is not uploaded twice"


def test_autofill_then_a_mandatory_letter_on_the_later_upload_box(client, fixture_url):
    applicant = make_applicant(client)
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/popup/letter/jobs/11"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app_id = response.json()["id"]
    app = wait_for(client, app_id, paused("approval"))
    assert "Cover letter included" in app["pause_reason"]
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"})
    wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED")
    assert state["submissions"][-1]["documents"] == ["Test_Person_Cover_Letter.docx"]


def test_thanks_beside_an_account_offer_counts_as_applied(client, fixture_url):
    applicant = make_applicant(client)
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/thanks/jobs/12"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app_id = response.json()["id"]
    wait_for(client, app_id, paused("approval"))
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"})
    app = wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED" or a["pause_kind"] == "account")
    assert app["status"] == "SUBMITTED" and app["submission_status"] == "Confirmed by the employer's page"


def test_workday_style_dropdowns_on_a_slow_form(client, fixture_url):
    applicant = make_applicant(client)
    response = client.put(f"/api/applicants/{applicant}", json={"profile": {**client.get("/api/applicants").json()[-1]["profile"], "state": "Illinois"}})
    assert response.status_code == 200
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/workday/jobs/7"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app_id = response.json()["id"]

    # Only the unknown required dropdown is asked, with its options read from the open list.
    app = wait_for(client, app_id, paused("questions"))
    assert [q["question"] for q in app["questions"]] == ["Phone Device Type", "Are you a current employee?"]
    assert [o["label"] if isinstance(o, dict) else o for o in app["questions"][0]["options"]][:2] == ["Mobile", "Landline"]
    client.post(f"/api/applications/{app_id}/answers", json={"reply": "1. Mobile, 2. No"})

    wait_for(client, app_id, paused("approval"))
    assert state["step1"]["state"] == "Illinois" and state["step1"]["device"] == "Mobile"
    assert state["submit_clicks"] == 0
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "hold"})
    wait_for(client, app_id, lambda a: a["status"] == "SAVED")


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def wait_for(client, app_id, predicate, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app = client.get(f"/api/applications/{app_id}").json()
        if predicate(app):
            return app
        time.sleep(0.25)
    raise AssertionError(f"Timed out. Last status {app['status']}/{app['pause_kind']}: {app['pause_reason']} | {app['log'][-5:]}")


def paused(kind):
    return lambda app: app["status"] == "PAUSED" and app["pause_kind"] == kind


def make_applicant(client, **extra):
    applicant = client.post("/api/applicants", json={"name": "Test Person"}).json()
    profile = {
        "first_name": "Test", "last_name": "Person", "email": "test.person@example.test",
        "phone": "+1 555 010 0100", "needs_sponsorship": "No", "min_salary": "100000",
        "notice_period": "2 weeks", "bonus_expectation": "No bonus expected", "how_heard": "LinkedIn",
        **extra,
    }
    client.put(f"/api/applicants/{applicant['id']}", json={"profile": profile})
    return applicant["id"]


def start(client, applicant_id, fixture_url):
    response = client.post(
        "/api/applications",
        data={"applicant_id": applicant_id, "job_url": f"{fixture_url}/jobs/1"},
        files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def sign_in_through_live_browser(client, app_id):
    """The person's clicks and keys, sent the way the dashboard's live view sends them."""
    frame = client.get(f"/api/applications/{app_id}/live.jpg")
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"
    live = f"/api/applications/{app_id}/live/input"
    client.post(live, json={"type": "key", "key": "Tab"})
    client.post(live, json={"type": "key", "key": "Tab"})
    client.post(live, json={"type": "text", "text": "test.person@example.test"})
    client.post(live, json={"type": "key", "key": "Tab"})
    client.post(live, json={"type": "text", "text": "not-a-real-password"})
    client.post(live, json={"type": "key", "key": "Enter"})


def answer_by_label(client, app_id, app, replies):
    answers = {}
    for question in app["questions"]:
        for fragment, value in replies.items():
            if fragment in question["question"].lower():
                answers[question["key"]] = value
    assert len(answers) == len(app["questions"]), f"Unexpected questions: {[q['question'] for q in app['questions']]}"
    response = client.post(f"/api/applications/{app_id}/answers", json={"answers": answers})
    assert response.status_code == 200, response.text


def drive_to_approval(client, fixture_url, applicant_id):
    app_id = start(client, applicant_id, fixture_url)

    wait_for(client, app_id, paused("account"))
    assert client.post(f"/api/applications/{app_id}/decision", json={"decision": "self"}).status_code == 200
    app = wait_for(client, app_id, paused("sign_in"))
    assert app["job"]["company"] == "Fixture Labs"
    assert app["job"]["title"] == "Site IT Engineer"
    assert app["job"]["salary_max"] == 150000
    assert app["screenshot"] == "", "nothing may be stored during sign-in"
    sign_in_through_live_browser(client, app_id)

    app = wait_for(client, app_id, paused("questions"))
    return app_id, app


def test_full_application_reaches_applied_only_after_approval(client, fixture_url):
    applicant_id = make_applicant(client)
    app_id, app = drive_to_approval(client, fixture_url, applicant_id)

    # Step 1: only what the profile cannot answer, numbered, with options listed.
    asked = {q["question"]: q for q in app["questions"]}
    assert set(asked) == {
        "Preferred name",
        "Any relatives employed by Fixture Labs?",
        "Are you subject to a non-compete agreement affecting this role?",
    }
    assert [q["number"] for q in app["questions"]] == [1, 2, 3]
    assert asked["Any relatives employed by Fixture Labs?"]["options"] == ["Yes", "No"]

    # The one-line reply format from the chat, with the blank that Croda went on to reject.
    numbers = {q["question"]: q["number"] for q in app["questions"]}
    reply = (f"{numbers['Preferred name']}. Leave blank, "
             f"{numbers['Any relatives employed by Fixture Labs?']}. No, "
             f"{numbers['Are you subject to a non-compete agreement affecting this role?']}. No")
    assert client.post(f"/api/applications/{app_id}/answers", json={"reply": reply}).status_code == 200

    app = wait_for(client, app_id, lambda a: paused("questions")(a) and "would not accept" in a["pause_reason"])
    assert [q["question"] for q in app["questions"]] == ["Preferred name"]
    assert "rejected it when blank" in app["questions"][0]["hint"]
    answer_by_label(client, app_id, app, {"preferred": "Test"})

    # Step 2: required demographics are asked with their options, never assumed.
    app = wait_for(client, app_id, lambda a: paused("questions")(a) and any("Gender" in q["question"] for q in a["questions"]))
    gender = next(q for q in app["questions"] if q["question"] == "Gender")
    assert gender["options"] == ["Male", "Female", "Do not wish to disclose"]
    answer_by_label(client, app_id, app, {"gender": "Female", "race": "Asian", "veteran": "None of the above"})

    # Step 3: the final step waits for an explicit yes.
    app = wait_for(client, app_id, paused("approval"))
    assert state["submit_clicks"] == 0
    assert "true and accurate" in app["pause_reason"]
    assert "searchable" in app["pause_reason"]
    assert app["status"] != "SUBMITTED"
    assert client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"}).status_code == 200

    app = wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED")
    assert app["submission_status"] == "Confirmed by the employer's page"
    assert app["progress"] == 100
    assert app["screenshot"]

    sent = state["submissions"][0]
    assert state["submit_clicks"] == 1
    assert sent["first_name"] == "Test" and sent["surname"] == "Person"
    assert sent["preferred"] == "Test"
    assert sent["email"] == sent["email2"] == "test.person@example.test"
    assert sent["sponsor"] == "no" and sent["relatives"] == "no" and sent["noncompete"] == "no"
    assert sent["salary"] == "145000", "advertised 150,000 minus 5,000, digits only"
    assert sent["notice"] == "2 weeks"
    assert sent["source"] == "LinkedIn"
    assert sent["gender"] == "f" and sent["race"] == "Asian" and sent["veteran"] == "None of the above"
    assert sent["resume_name"] == "Test_Resume.pdf" and sent["resume_size"] > 0
    assert sent["city"] == "", "the wrong value from the employer's parser is cleared"
    assert "searchable" not in sent, "optional opt-in stays unchecked"

    counts = client.get("/api/applications", params={"applicant_id": applicant_id}).json()["counts"]
    assert counts["applied"] == 1 and counts["paused"] == 0

    # Second job: saved sign-in and saved answers mean only the approval is left to ask.
    second = start(client, applicant_id, fixture_url)
    app = wait_for(client, second, paused("approval"))
    assert state["submit_clicks"] == 1
    assert client.post(f"/api/applications/{second}/decision", json={"decision": "hold"}).status_code == 200
    app = wait_for(client, second, lambda a: a["status"] == "SAVED")
    assert app["submission_status"] == "Held by you, not submitted"
    assert state["submit_clicks"] == 1, "a hold must not submit"


def test_applicants_do_not_share_sign_ins_or_answers(client, fixture_url):
    first = make_applicant(client)
    app_id, app = drive_to_approval(client, fixture_url, first)
    answer_by_label(client, app_id, app, {"preferred": "Test", "relatives": "No", "non-compete": "No"})
    wait_for(client, app_id, lambda a: paused("questions")(a) and any("Gender" in q["question"] for q in a["questions"]))

    other = make_applicant(client, first_name="Other")
    other_app = start(client, other, fixture_url)
    wait_for(client, other_app, paused("account"))
    assert client.get(f"/api/applicants/{other}/answers").json() == []


def test_write_requests_need_the_app_header(client):
    from fastapi.testclient import TestClient

    from app.main import app
    bare = TestClient(app)
    assert bare.post("/api/applicants", json={"name": "x"}).status_code == 403


@pytest.mark.parametrize("bad", ["ftp://example.test/job", "not a url", "javascript:alert(1)"])
def test_bad_job_urls_are_rejected(client, bad):
    applicant_id = make_applicant(client)
    response = client.post("/api/applications", data={"applicant_id": applicant_id, "job_url": bad},
                           files={"file": ("r.pdf", pdf_bytes(), "application/pdf")})
    assert response.status_code == 422


def test_only_pdf_or_docx_resumes(client):
    applicant_id = make_applicant(client)
    response = client.post(f"/api/applicants/{applicant_id}/resumes",
                           files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert response.status_code == 422


def test_an_upload_button_that_opens_a_file_picker_gets_the_resume(client, fixture_url):
    applicant = make_applicant(client)
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/picker/jobs/8"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app_id = response.json()["id"]
    wait_for(client, app_id, paused("questions"))
    # The person clicks the button in the live browser, and the picker that opens is filled for them.
    assert client.post(f"/api/applications/{app_id}/live/input", json={"type": "click", "x": 100, "y": 30}).status_code == 200
    app = wait_for(client, app_id, lambda a: any("through the site's file picker" in e["message"] for e in a["log"]), timeout=15)
    assert any("Uploaded resume Test_Resume.pdf through the site's file picker" in e["message"] for e in app["log"])
    client.post(f"/api/applications/{app_id}/answers", json={"reply": "1. Python"})
    wait_for(client, app_id, paused("approval"))
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"})
    wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED")
    assert state["submissions"][-1]["uploaded"] == "Test_Resume.pdf"


def test_working_in_the_live_browser_keeps_it_open(client, fixture_url, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "PAUSE_TIMEOUT_SECONDS", 3)
    applicant = make_applicant(client)
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/picker/jobs/8"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app_id = response.json()["id"]
    wait_for(client, app_id, paused("questions"))
    for _ in range(5):
        assert client.post(f"/api/applications/{app_id}/live/input", json={"type": "scroll", "x": 10, "y": 10, "dy": 0}).status_code == 200
        time.sleep(1)
    assert client.get(f"/api/applications/{app_id}").json()["live"], "typing in the live browser is not idle time"
    app = wait_for(client, app_id, lambda a: not a["live"], timeout=15)
    assert any("Browser released after waiting" in e["message"] for e in app["log"])


def test_a_checkbox_option_with_a_comma_is_one_choice(client, fixture_url):
    applicant = make_applicant(client)
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/group/jobs/9"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app_id = response.json()["id"]
    app = wait_for(client, app_id, paused("questions"))
    assert [q["question"] for q in app["questions"]] == ["Have you migrated users between SaaS platforms?"]
    client.post(f"/api/applications/{app_id}/answers", json={"answers": {app["questions"][0]["key"]: "No, I don't have migration experience"}})
    wait_for(client, app_id, paused("approval"))
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"})
    wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED")
    assert state["submissions"][-1]["mig"] == ["none"]
