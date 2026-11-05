"""Emailed verification codes: which mail is trusted, what is stored, and the real browser flow."""
import imaplib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import config, db, mailbox, vault
from tests.fixture_site import code_email, inbox, link_email, linkco, otp, portal, state
from tests.test_flow import make_applicant, paused, pdf_bytes, wait_for

APP_PASSWORD = "abcd efgh ijkl mnop"
EMAIL = "test.person@example.test"


class FakeImap:
    """Stands in for the provider and records how the mailbox was used."""
    calls: list = []
    good_password = "abcdefghijklmnop"

    def __init__(self, host, timeout=None):
        self.calls.append(("connect", host))

    def login(self, address, password):
        if password != self.good_password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, name, readonly=False):
        self.calls.append(("select", name, readonly))
        return "OK", [b"1"]

    def search(self, charset, criteria):
        self.calls.append(("search", criteria))
        domain = criteria.split('FROM "')[1].rstrip('")')
        numbers = [str(i + 1).encode() for i, raw in enumerate(inbox) if domain.encode() in raw.split(b"\r\n")[0]]
        return "OK", [b" ".join(numbers)]

    def fetch(self, number, parts):
        self.calls.append(("fetch", parts))
        return "OK", [(b"1 (BODY[] {1})", inbox[int(number) - 1]), b")"]

    def logout(self):
        pass


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    FakeImap.calls = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr(config, "CODE_WAIT_SECONDS", 2)
    monkeypatch.setattr(config, "CODE_POLL_SECONDS", 1)


def connect(client, applicant):
    response = client.put(f"/api/applicants/{applicant}/mailbox",
                          json={"address": "Person@Gmail.com", "app_password": APP_PASSWORD})
    assert response.status_code == 200, response.text
    return response


def add_login(applicant, fixture_url):
    portal["users"][EMAIL] = "the-real-password"
    vault.save(applicant, vault.site_key(fixture_url), EMAIL, "the-real-password")


def start(client, applicant, url):
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": url},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.parametrize("subject, body, expected", [
    ("Verify", "Your verification code is 482913.", "482913"),
    ("Verify", "<p>Your security code:</p><div><b>A7K2QX</b></div>", "A7K2QX"),
    ("482913 is your code", "", "482913"),
    ("Sign in", "Your one-time password: 77120", "77120"),
    ("Verify", "Your code is 482913. Enter 482913 to continue.", "482913"),
    ("Verify", "Your code is 111111. The code 222222 is old.", None),
    ("Verify", "The code expires in 10 minutes.", None),
    ("Your application", "Thanks for applying to job 123456 at Acme.", None),
    ("Offices", "Our postal code 94103 is on the map.", None),
])
def test_code_extraction_never_guesses(subject, body, expected):
    assert mailbox.extract_code(subject, body) == expected


def test_only_the_asking_site_can_supply_a_code():
    allowed = mailbox.sender_domains("https://careers.acme.com/verify")
    assert mailbox.sent_by("Acme <no-reply@acme.com>", allowed)
    assert mailbox.sent_by("no-reply@mail.acme.com", allowed)
    assert not mailbox.sent_by("no-reply@notacme.com", allowed)
    assert not mailbox.sent_by("no-reply@acme.com.evil.test", allowed)
    assert not mailbox.sent_by("acme.com <no-reply@evil.test>", allowed)
    assert not mailbox.sent_by("no-reply@accounts.google.com", allowed)
    assert mailbox.sent_by("a@myworkday.com", mailbox.sender_domains("https://acme.wd5.myworkdayjobs.com/x"))
    assert mailbox.sender_domains("https://careers.acme.co.uk/x") == {"acme.co.uk"}


def test_a_bad_login_saves_nothing_and_secrets_stay_out_of_the_api(client):
    applicant = make_applicant(client)
    refused = client.put(f"/api/applicants/{applicant}/mailbox",
                         json={"address": "person@gmail.com", "app_password": "wrong"})
    assert refused.status_code == 422 and "refused" in refused.text
    assert client.get(f"/api/applicants/{applicant}/mailbox").json()["mailbox"] is None

    response = connect(client, applicant)
    assert FakeImap.good_password not in response.text and "secret" not in response.text
    assert response.json()["mailbox"]["address"] == "person@gmail.com"
    stored = bytes(db.one("SELECT secret FROM mailboxes WHERE applicant_id=?", applicant)["secret"])
    assert FakeImap.good_password.encode() not in stored

    client.delete(f"/api/applicants/{applicant}/mailbox")
    assert client.get(f"/api/applicants/{applicant}/mailbox").json()["mailbox"] is None


def test_unsupported_providers_are_refused(client):
    applicant = make_applicant(client)
    response = client.put(f"/api/applicants/{applicant}/mailbox",
                          json={"address": "person@evil.test", "app_password": APP_PASSWORD})
    assert response.status_code == 422 and "not supported" in response.text


def test_mailbox_is_read_only_and_only_the_sites_mail_is_downloaded(client):
    applicant = make_applicant(client)
    connect(client, applicant)
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=5)
    inbox.extend([
        code_email("111111", "security@bank.test"),
        code_email("222222", "no-reply@acme.test", now - timedelta(hours=2)),
        code_email("333333", "no-reply@acme.test.evil.test"),
        code_email("444444", "no-reply@acme.test", now - timedelta(minutes=2)),
        code_email("555555", "no-reply@acme.test"),
    ])
    assert mailbox.fetch_code(applicant, "https://careers.acme.test/v", since, set()) == "555555"
    assert mailbox.fetch_code(applicant, "https://careers.acme.test/v", since, {"555555"}) == "444444"
    assert mailbox.fetch_code(applicant, "https://careers.acme.test/v", since, {"555555", "444444"}) is None

    assert ("select", "INBOX", True) in FakeImap.calls
    assert all(call[1] == "(BODY.PEEK[])" for call in FakeImap.calls if call[0] == "fetch")
    fetched = [call for call in FakeImap.calls if call[0] == "fetch"]
    assert len(fetched) == 3 * 4, "the bank's email is never downloaded"


def test_code_is_entered_automatically_and_never_recorded(client, fixture_url):
    applicant = make_applicant(client)
    connect(client, applicant)
    add_login(applicant, fixture_url)
    app_id = start(client, applicant, f"{fixture_url}/otp/jobs/3")

    app = wait_for(client, app_id, paused("approval"))
    assert otp["accepted"] == [otp["code"]] and otp["rejected"] == []
    assert state["submit_clicks"] == 0, "a code never replaces the person's yes"
    assert any("Entered the code emailed by 127.0.0.1" in e["message"] for e in app["log"])
    assert otp["code"] not in json.dumps(client.get(f"/api/applications/{app_id}").json())
    if not config.DATABASE_URL:
        raw = sqlite3.connect(config.DB_PATH)
        dump = "\n".join(raw.iterdump())
        raw.close()
        assert otp["code"] not in dump

    client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"})
    wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED")


def test_without_a_connected_email_it_asks_the_person(client, fixture_url):
    applicant = make_applicant(client)
    add_login(applicant, fixture_url)
    app_id = start(client, applicant, f"{fixture_url}/otp/jobs/3")
    app = wait_for(client, app_id, paused("help"))
    assert "wants a code" in app["pause_reason"] and "Connect an email" in app["pause_reason"]
    assert otp["accepted"] == []
    client.post(f"/api/applications/{app_id}/mark-applied")


def test_a_code_from_the_wrong_sender_is_never_typed(client, fixture_url):
    applicant = make_applicant(client)
    connect(client, applicant)
    add_login(applicant, fixture_url)
    # The site's own email goes missing, so only another sender's code is in the inbox.
    otp["send_email"] = False
    inbox.append(code_email("999999", "security@bank.test"))
    app_id = start(client, applicant, f"{fixture_url}/otp/jobs/3")
    app = wait_for(client, app_id, paused("help"))
    assert "No usable code" in app["pause_reason"]
    assert otp["rejected"] == [] and otp["accepted"] == []
    client.post(f"/api/applications/{app_id}/mark-applied")


def test_a_rejected_code_stops_after_two_tries(client, fixture_url):
    applicant = make_applicant(client)
    connect(client, applicant)
    add_login(applicant, fixture_url)
    # Every code in the inbox is wrong, and a site can lock an account after too many.
    otp["emailed"] = "100001"
    inbox.append(code_email("100002", "no-reply@127.0.0.1", datetime.now(timezone.utc) - timedelta(seconds=30)))
    app_id = start(client, applicant, f"{fixture_url}/otp/jobs/3")
    app = wait_for(client, app_id, paused("help"))
    assert otp["rejected"] == ["100001", "100002"]
    assert "still wants a code" in app["pause_reason"]
    client.post(f"/api/applications/{app_id}/mark-applied")


def test_a_code_on_the_final_form_does_not_click_submit(client, fixture_url):
    applicant = make_applicant(client)
    connect(client, applicant)
    app_id = start(client, applicant, f"{fixture_url}/otp/final/jobs/4")
    app = wait_for(client, app_id, paused("approval"))
    assert state["submit_clicks"] == 0
    assert any("Entered the code" in e["message"] for e in app["log"])
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "submit"})
    wait_for(client, app_id, lambda a: a["status"] == "SUBMITTED")
    assert state["submissions"][-1]["code"] == otp["code"]


@pytest.mark.parametrize("body, expected", [
    ('<a href="https://careers.acme.com/verify?t=1">Verify my email</a>', "https://careers.acme.com/verify?t=1"),
    ('<a href="https://acme.com/a/9f2">Confirm your account</a> <a href="https://acme.com/unsubscribe">Unsubscribe</a>',
     "https://acme.com/a/9f2"),
    ("Activate your account: https://jobs.acme.com/activate/abc.", "https://jobs.acme.com/activate/abc"),
    ('<a href="https://click.mailer.test/r?u=acme.com/verify">Verify</a>', None),
    ('<a href="https://acme.com.evil.test/verify">Verify</a>', None),
    ('<a href="https://acme.com/verify/1">Verify</a> <a href="https://acme.com/verify/2">Verify</a>', None),
    ('<a href="https://acme.com/jobs/12">See more jobs</a>', None),
    ('<a href="javascript:alert(1)">Verify</a>', None),
])
def test_verification_link_stays_on_the_sites_own_domain(body, expected):
    assert mailbox.extract_link(body, mailbox.sender_domains("https://careers.acme.com/register")) == expected


def test_new_account_is_verified_by_email_link_then_signs_in(client, fixture_url):
    applicant = make_applicant(client)
    connect(client, applicant)
    client.put(f"/api/applicants/{applicant}/auto-accounts", json={"enabled": True})
    inbox.append(link_email("http://127.0.0.1:1/link/verify?token=wrong", "security@bank.test"))
    app_id = start(client, applicant, f"{fixture_url}/link/jobs/7")

    app = wait_for(client, app_id, paused("approval"))
    assert EMAIL in linkco["verified"]
    messages = [e["message"] for e in app["log"]]
    assert any("Opened the verification link emailed by 127.0.0.1" in m for m in messages)
    assert any("Signed in to 127.0.0.1 with the saved password" in m for m in messages)
    assert vault.list_for(applicant)[0]["status"] == vault.STATUS_ACTIVE
    assert linkco["token"] not in json.dumps(client.get(f"/api/applications/{app_id}").json())
    assert state["submit_clicks"] == 0, "verifying an account never replaces the person's yes"
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "hold"})
    wait_for(client, app_id, lambda a: a["status"] == "SAVED")


def test_without_a_verification_email_it_asks_the_person(client, fixture_url):
    applicant = make_applicant(client)
    connect(client, applicant)
    client.put(f"/api/applicants/{applicant}/auto-accounts", json={"enabled": True})
    linkco["send_email"] = False
    app_id = start(client, applicant, f"{fixture_url}/link/jobs/7")
    app = wait_for(client, app_id, paused("help"))
    assert "No verification link" in app["pause_reason"]
    assert linkco["verified"] == set()
    client.post(f"/api/applications/{app_id}/mark-applied")
