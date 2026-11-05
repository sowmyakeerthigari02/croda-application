"""Claude response handling, with the network call faked. No API key or spend involved."""
import json
from types import SimpleNamespace

import anthropic
import pytest

from app import ai


def reply(payload, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=json.dumps(payload))])


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = {}

    def install(result):
        def create(**kwargs):
            calls.update(kwargs)
            if isinstance(result, Exception):
                raise result
            return result
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
        return calls

    return install


def test_supported_answer_is_used(fake_claude):
    calls = fake_claude(reply({"answerable": True, "answer": "Five years running Windows Server."}))
    assert ai.draft_answer("Describe your experience", {}, "resume text") == "Five years running Windows Server."
    assert calls["model"] == "claude-opus-5-5"
    assert calls["fallbacks"] == "default"


def test_unsupported_question_goes_to_the_person(fake_claude):
    fake_claude(reply({"answerable": False, "answer": ""}))
    assert ai.draft_answer("Do you need sponsorship?", {}, "resume text") is None


def test_refusal_goes_to_the_person(fake_claude):
    fake_claude(reply({"answerable": True, "answer": "x"}, stop_reason="refusal"))
    assert ai.draft_answer("Describe your experience", {}, "resume text") is None


def test_no_key_means_no_call(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ai.draft_answer("Describe your experience", {}, "resume text") is None


def test_job_details_fall_back_to_the_page_title(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    job = ai.analyze_job("Pay is $120,000 - $135,000.", "Acme hiring Network Engineer in Andover, MA | LinkedIn", "https://x.test")
    assert (job["company"], job["title"], job["location"]) == ("Acme", "Network Engineer", "Andover, MA")
    assert (job["salary_min"], job["salary_max"]) == (120000, 135000)
