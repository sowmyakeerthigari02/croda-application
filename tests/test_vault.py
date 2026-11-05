"""The password vault: encryption, separation, and automatic sign-in and sign-up in a real browser."""
import json
import re
import sqlite3

from app import config, db, forms, vault, worker
from tests.fixture_site import portal, state
from tests.test_flow import make_applicant, paused, pdf_bytes, wait_for


def start_portal(client, applicant_id, fixture_url):
    response = client.post(
        "/api/applications",
        data={"applicant_id": applicant_id, "job_url": f"{fixture_url}/portal/jobs/2"},
        files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def forget_browser_sign_in(applicant_id):
    db.execute("UPDATE applicants SET browser_session=NULL WHERE id=?", applicant_id)


def test_sites_are_matched_exactly():
    assert vault.site_key("https://careers.acme.com/jobs/1") == "careers.acme.com"
    assert vault.site_key("careers.acme.com") == "careers.acme.com"
    assert vault.site_key("https://careers.acme.com.evil.test/login") != "careers.acme.com"


def test_employers_sharing_a_portal_host_stay_separate():
    one = vault.site_key("https://career4.successfactors.com/career?company=Vicor&x=1")
    two = vault.site_key("https://career4.successfactors.com/career?company=Acme")
    assert one != two and "vicor" in one


def test_generated_passwords_are_unique_and_fit_common_rules():
    made = {vault.generate_password() for _ in range(200)}
    assert len(made) == 200
    for password in made:
        assert 8 <= len(password) <= 18 and " " not in password
        assert re.search("[A-Z]", password) and re.search("[a-z]", password) and re.search(r"\d", password)
        assert re.search(r"[^A-Za-z0-9]", password)


def test_password_is_encrypted_and_the_api_never_returns_it(client):
    applicant = make_applicant(client)
    secret = "Unique-test-Pass-77"
    response = client.post(f"/api/applicants/{applicant}/credentials",
                           json={"site": "https://careers.acme.test/x", "login_email": "A@Example.test", "password": secret})
    assert response.status_code == 200
    assert secret not in response.text
    listed = client.get(f"/api/applicants/{applicant}/credentials")
    assert secret not in listed.text
    assert listed.json()[0]["site"] == "careers.acme.test"
    assert "secret" not in listed.json()[0] and "password" not in listed.json()[0]

    stored = db.one("SELECT secret FROM credentials WHERE applicant_id=?", applicant)["secret"]
    assert secret.encode() not in bytes(stored)
    assert vault.find(applicant, "careers.acme.test", "a@example.test")["password"] == secret


def test_applicants_cannot_reach_each_others_passwords(client):
    first, second = make_applicant(client), make_applicant(client)
    client.post(f"/api/applicants/{first}/credentials",
                json={"site": "careers.acme.test", "login_email": "a@example.test", "password": "first-pass"})
    assert vault.find(second, "careers.acme.test", "a@example.test") is None
    credential = vault.list_for(first)[0]
    client.delete(f"/api/applicants/{second}/credentials/{credential['id']}")
    assert vault.list_for(first), "another applicant's delete must do nothing"


def test_two_accounts_on_a_site_are_not_guessed_between(client):
    applicant = make_applicant(client)
    vault.save(applicant, "careers.acme.test", "a@example.test", "pass-a")
    vault.save(applicant, "careers.acme.test", "b@example.test", "pass-b")
    assert vault.find(applicant, "careers.acme.test", "b@example.test")["password"] == "pass-b"
    assert vault.find(applicant, "careers.acme.test", "c@example.test") is None


def test_new_account_then_automatic_sign_in_on_the_next_job(client, fixture_url):
    applicant = make_applicant(client)
    app_id = start_portal(client, applicant, fixture_url)

    wait_for(client, app_id, paused("account"))
    assert not portal["users"], "no account before the person says so"
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "create"})

    # The privacy terms box is the person's to accept, and nothing else is asked.
    app = wait_for(client, app_id, paused("questions"))
    assert [q["question"] for q in app["questions"]] == ["I agree to the data privacy terms"]
    client.post(f"/api/applications/{app_id}/answers", json={"reply": "1. Yes"})

    app = wait_for(client, app_id, paused("approval"))
    password = portal["users"]["test.person@example.test"]
    assert 8 <= len(password) <= 18
    assert vault.find(applicant, "127.0.0.1", "test.person@example.test")["password"] == password
    assert state["submit_clicks"] == 0
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"})
    wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED")

    # The password appears nowhere readable: API responses, the log, saved answers or raw database text.
    assert password not in json.dumps(client.get(f"/api/applications/{app_id}").json())
    assert password not in client.get(f"/api/applicants/{applicant}/credentials").text
    assert password not in client.get(f"/api/applicants/{applicant}/answers").text
    if not config.DATABASE_URL:
        raw = sqlite3.connect(config.DB_PATH)
        dump = "\n".join(raw.iterdump())
        raw.close()
        assert password not in dump

    # A later job with no saved browser session signs in with no questions.
    forget_browser_sign_in(applicant)
    second = start_portal(client, applicant, fixture_url)
    app = wait_for(client, second, paused("approval"))
    assert any("Signed in to 127.0.0.1 with the saved password" in entry["message"] for entry in app["log"])
    assert vault.list_for(applicant)[0]["status"] == vault.STATUS_ACTIVE
    client.post(f"/api/applications/{second}/decision", json={"decision": "hold"})
    wait_for(client, second, lambda a: a["status"] == "SAVED")


def test_automatic_accounts_are_off_until_the_person_turns_them_on(client):
    applicant = make_applicant(client)
    assert client.get(f"/api/applicants/{applicant}/auto-accounts").json()["since"] is None
    assert client.put(f"/api/applicants/{applicant}/auto-accounts", json={"enabled": True}).json()["since"]
    assert client.put(f"/api/applicants/{applicant}/auto-accounts", json={"enabled": False}).json()["since"] is None


def test_automatic_account_needs_no_clicks_but_submitting_still_does(client, fixture_url):
    applicant = make_applicant(client)
    client.put(f"/api/applicants/{applicant}/auto-accounts", json={"enabled": True})
    app_id = start_portal(client, applicant, fixture_url)

    app = wait_for(client, app_id, paused("approval"))
    password = portal["users"]["test.person@example.test"]
    assert vault.find(applicant, "127.0.0.1", "test.person@example.test")["password"] == password
    assert portal["registration"]["terms"] == "1" and "alerts" not in portal["registration"]
    assert any('Accepted on 127.0.0.1: "I agree to the data privacy terms"' in e["message"] for e in app["log"])
    assert password not in json.dumps(app)
    assert state["submit_clicks"] == 0, "automatic accounts never replace the person's yes"
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "hold"})
    wait_for(client, app_id, lambda a: a["status"] == "SAVED")


def test_a_sign_in_popup_pauses_instead_of_timing_out(client, fixture_url, monkeypatch):
    monkeypatch.setattr(worker, "NEVER_NEW_ACCOUNT", {"127.0.0.1"})
    applicant = make_applicant(client)
    client.put(f"/api/applicants/{applicant}/auto-accounts", json={"enabled": True})
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/board/jobs/5"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app_id = response.json()["id"]
    app = wait_for(client, app_id, paused("sign_in"))
    assert "sign in" in app["pause_reason"].lower()
    assert not vault.list_for(applicant), "job boards and personal accounts never get new accounts"
    client.post(f"/api/applications/{app_id}/mark-applied")


def test_only_terms_boxes_are_accepted_automatically():
    def box(label):
        return {"kind": "checkbox", "label": label}
    assert forms.is_terms_box(box("I agree to the Terms and Conditions"))
    assert forms.is_terms_box(box("I have read the Privacy Policy"))
    assert not forms.is_terms_box(box("Send me offers, subject to the terms"))
    assert not forms.is_terms_box(box("I certify my answers are true, under these terms"))
    assert not forms.is_terms_box(box("I am authorized to work in the US"))
    assert not forms.is_terms_box(box("Consider me for other positions"))
    assert not forms.is_terms_box({"kind": "text", "label": "Terms"})


def test_wrong_saved_password_tries_once_then_hands_over(client, fixture_url):
    applicant = make_applicant(client)
    portal["users"]["test.person@example.test"] = "the-real-password"
    client.post(f"/api/applicants/{applicant}/credentials",
                json={"site": fixture_url, "login_email": "test.person@example.test", "password": "a-wrong-password"})
    app_id = start_portal(client, applicant, fixture_url)
    app = wait_for(client, app_id, paused("sign_in"))
    assert "did not finish signing in" in app["pause_reason"]
    assert vault.list_for(applicant)[0]["status"] == vault.STATUS_FAILED
    attempts = [e for e in app["log"] if "did not work" in e["message"]]
    assert len(attempts) == 1
    assert "a-wrong-password" not in json.dumps(app)
    client.post(f"/api/applications/{app_id}/mark-applied")


def test_an_unfinished_account_does_not_block_making_a_new_one(client, fixture_url):
    applicant = make_applicant(client)
    vault.save(applicant, vault.site_key(fixture_url), "test.person@example.test", "never-registered-1", status=vault.STATUS_PENDING)
    app_id = start_portal(client, applicant, fixture_url)
    app = wait_for(client, app_id, paused("account"))
    assert vault.list_for(applicant) == []
    assert any("never finished" in e["message"] for e in app["log"])
    client.post(f"/api/applications/{app_id}/mark-applied")


def test_passwords_stay_off_plain_http_in_production(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", False)
    assert not vault.usable_on("http://careers.acme.test/login")
    assert vault.usable_on("https://careers.acme.test/login")


def test_a_saved_password_switches_from_sign_up_to_sign_in(client, fixture_url):
    applicant = make_applicant(client)
    portal["users"]["test.person@example.test"] = "the-real-password"
    client.post(f"/api/applicants/{applicant}/credentials",
                json={"site": fixture_url, "login_email": "test.person@example.test", "password": "the-real-password"})
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": f"{fixture_url}/wd/jobs/10"},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    app = wait_for(client, response.json()["id"], paused("approval"))
    assert any("Signed in to 127.0.0.1 with the saved password" in e["message"] for e in app["log"])
    client.post(f"/api/applications/{app['id']}/decision", json={"decision": "hold"})
