"""Cover letters are created and attached only when a job makes them mandatory."""
import io

import pytest
from docx import Document

from app import letters
from tests.fixture_site import state
from tests.test_flow import make_applicant, paused, pdf_bytes, wait_for


@pytest.mark.parametrize("text, expected", [
    ("ATTN: To be considered, resume and cover letter are required.", True),
    ("You must include a cover letter.", True),
    ("Please attach a resume and cover letter", True),
    ("A cover letter is optional.", False),
    ("Cover letter not required.", False),
    ("You may include a cover letter if you wish.", False),
    ("Cover letters are encouraged.", False),
    ("Our team read every cover letter last year.", False),
    ("A resume is required. References are welcome.", False),
])
def test_only_a_mandatory_letter_counts(text, expected):
    assert letters.required_by_posting(text) is expected


def test_generated_letter_uses_only_the_resume_summary():
    resume = "SAM\nProfessional Summary\nAdministrator with 5 years on VMware.\nTechnical Skills\nPowerShell"
    profile = {"first_name": "Sam", "last_name": "Lee", "email": "s@example.test"}
    text = letters.write(profile, {"title": "IT Admin | Acme", "company": "Acme"}, resume)
    assert "IT Admin position at Acme" in text and "Administrator with 5 years on VMware." in text
    assert "Sincerely,\nSam Lee" in text and "PowerShell" not in text
    assert letters.filename(profile) == "Sam_Lee_Cover_Letter.docx"
    assert "Administrator with 5 years" in "\n".join(p.text for p in Document(io.BytesIO(letters.to_docx(text))).paragraphs)


def start(client, applicant, url):
    response = client.post("/api/applications", data={"applicant_id": applicant, "job_url": url},
                           files={"file": ("Test_Resume.pdf", pdf_bytes(), "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def finish(client, app_id):
    client.post(f"/api/applications/{app_id}/decision", json={"decision": "hold"})
    wait_for(client, app_id, lambda a: a["status"] == "SAVED")


def test_mandatory_letter_goes_in_the_one_upload_box(client, fixture_url):
    applicant = make_applicant(client)
    app_id = start(client, applicant, f"{fixture_url}/letter/required/jobs/8")
    app = wait_for(client, app_id, paused("approval"))
    assert state["step1"]["documents"] == ["Test_Resume.pdf", "Test_Person_Cover_Letter.docx"]
    assert "Cover letter included: Test_Person_Cover_Letter.docx" in app["pause_reason"]
    downloaded = client.get(f"/api/applications/{app_id}/cover-letter")
    assert downloaded.status_code == 200 and downloaded.content[:2] == b"PK"
    assert state["submit_clicks"] == 0
    finish(client, app_id)


def test_optional_letter_stays_blank(client, fixture_url):
    applicant = make_applicant(client)
    app_id = start(client, applicant, f"{fixture_url}/letter/optional/jobs/9")
    app = wait_for(client, app_id, paused("approval"))
    assert state["step1"] == {"documents": ["Test_Resume.pdf"], "cover": "", "cover_text": ""}
    assert "cover_letter" not in app["job"] and "Cover letter" not in app["pause_reason"]
    assert client.get(f"/api/applications/{app_id}/cover-letter").status_code == 404
    finish(client, app_id)


def test_a_required_box_on_the_form_gets_a_letter(client, fixture_url):
    applicant = make_applicant(client)
    app_id = start(client, applicant, f"{fixture_url}/letter/form-only/jobs/10")
    wait_for(client, app_id, paused("approval"))
    assert state["step1"]["documents"] == ["Test_Resume.pdf"]
    assert state["step1"]["cover"] == "Test_Person_Cover_Letter.docx"
    finish(client, app_id)


def test_the_persons_own_letter_replaces_a_generated_one(client, fixture_url):
    applicant = make_applicant(client)
    app_id = start(client, applicant, f"{fixture_url}/letter/required/jobs/8")
    wait_for(client, app_id, paused("approval"))
    document = Document()
    document.add_paragraph("My own words.")
    buffer = io.BytesIO()
    document.save(buffer)
    response = client.post(f"/api/applications/{app_id}/cover-letter",
                           files={"file": ("Mine.docx", buffer.getvalue(), "application/octet-stream")})
    assert response.status_code == 200
    assert response.json()["job"]["cover_letter"]["filename"] == "Mine.docx"
    assert "My own words." in response.json()["job"]["cover_letter"]["text"]
    finish(client, app_id)
