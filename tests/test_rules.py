from datetime import date

from app import rules


def options(*labels):
    return [{"label": label, "value": label} for label in labels]


def test_chat_reply_format_parses():
    reply = "1. No, 2. Leave blank, 3. USD 145,000 per year"
    assert rules.parse_numbered_reply(reply, 3) == {1: "No", 2: "Leave blank", 3: "USD 145,000 per year"}


def test_reply_ignores_numbers_beyond_the_question_count():
    assert rules.parse_numbered_reply("1. Suite 9. Main St", 1) == {1: "Suite 9. Main St"}


def test_male_never_matches_female():
    assert rules.match_option("Female", options("Male", "Female"))["label"] == "Female"
    assert rules.match_option("Male", options("Male", "Female"))["label"] == "Male"


def test_ambiguous_answer_is_not_guessed():
    assert rules.match_option("Yes", options("Yes, now", "Yes, later", "No")) is None


def test_negated_answer_does_not_match_the_positive_option():
    assert rules.match_option("Not Hispanic or Latino", options("Hispanic or Latino", "Asian")) is None


def test_decline_wordings_match():
    assert rules.match_option("Do not wish to disclose", options("Male", "I prefer not to say"))["label"] == "I prefer not to say"


def test_a_posted_salary_wins_over_the_profile():
    profile = {"min_salary": "100000"}
    assert rules.salary_value(profile, {"salary_min": 140000, "salary_max": 150000}, "text") == "145000"
    assert rules.salary_value(profile, {"salary_max": 90000}, "text") == "85000"
    assert rules.salary_value({}, {"salary_max": 150000}, "text") == "145000"
    # A narrow range or a single figure never falls below what they posted.
    assert rules.salary_value(profile, {"salary_min": 82000, "salary_max": 86000}, "text") == "82000"
    assert rules.salary_value(profile, {"salary_min": 90000, "salary_max": 90000}, "text") == "90000"


def test_with_no_posted_salary_the_profile_figure_is_used():
    profile = {"min_salary": "100000"}
    assert rules.salary_value(profile, {}, "text") == "100000"
    assert rules.salary_value(profile, {}, "textarea") == "USD 100,000 per year"
    assert rules.salary_value({}, {}, "text") is None


def test_availability_counts_application_day_as_day_one():
    assert rules.availability_date("2 weeks", date(2026, 10, 1)) == date(2026, 10, 14)


def test_availability_moves_weekend_to_monday():
    assert rules.availability_date("2 weeks", date(2026, 10, 4)) == date(2026, 10, 19)  # lands on Saturday 17th


def test_date_field_gets_a_date_and_text_field_gets_the_period():
    profile = {"notice_period": "2 weeks"}
    today = date(2026, 10, 1)
    assert rules.resolve({"label": "Notice period", "kind": "text"}, profile, {}, "https://x.test", today) == "2 weeks"
    assert rules.resolve({"label": "Available start date", "kind": "date"}, profile, {}, "https://x.test", today) == "2026-10-14"


def test_classifier_keeps_similar_labels_apart():
    assert rules.classify("Preferred name") == "preferred_name"
    assert rules.classify("Surname") == "last_name"
    assert rules.classify("Retype Email Address") == "email"
    assert rules.classify("Capacity") is None
    assert rules.classify("Will you need US visa sponsorship now or in the future?") == "needs_sponsorship"
    assert rules.classify("Any relatives employed by Acme?") is None


def test_source_is_linkedin_only_for_linkedin_links():
    field = {"label": "How did you hear about us?", "kind": "select"}
    assert rules.resolve(field, {}, {}, "https://www.linkedin.com/jobs/view/1") == "LinkedIn"
    assert rules.resolve(field, {}, {}, "https://careers.acme.test/1") is None


def test_invalid_choice_is_rejected_with_the_options():
    question = {"question": "Gender", "options": ["Male", "Female"]}
    try:
        rules.normalize_answer(question, "Other")
    except ValueError as exc:
        assert "Male / Female" in str(exc)
    else:
        raise AssertionError("expected a ValueError")


def test_phone_details_are_not_the_phone_number():
    assert rules.classify("Phone Number") == "phone"
    for label in ("Phone Extension", "Phone Device Type", "Country Phone Code"):
        assert rules.classify(label) != "phone"
