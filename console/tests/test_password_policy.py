"""U3 / CONTRACTS §12.1: one place decides password and key strength, and set/change rejects weak ones with a
422 that names the rule."""

import re

import pytest

from ramen_console.errors import ApiError
from ramen_console.security import (
    MIN_LEN,
    PASSWORD_RULE,
    check_password,
    generate_key_secret,
    generate_password,
    min_length,
)

CLASSES = (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"), re.compile(r"[^A-Za-z0-9]"))


def strong(s: str, least: int = MIN_LEN) -> bool:
    return len(s) >= least and all(c.search(s) for c in CLASSES)


def test_rule_text_states_every_requirement():
    for word in ("12", "upper", "lower", "digit", "special"):
        assert word in PASSWORD_RULE.lower()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Sh0rt!",  # < 12
        "alllowercase1!",  # no upper
        "ALLUPPERCASE1!",  # no lower
        "NoDigitsHere!!",  # no digit
        "NoSpecials1234",  # no special
    ],
)
def test_weak_passwords_are_rejected_with_a_422_naming_the_rule(bad):
    with pytest.raises(ApiError) as e:
        check_password(bad)
    assert e.value.status_code == 422
    assert PASSWORD_RULE in e.value.detail


def test_a_strong_password_passes():
    check_password("Passw0rd!-for-tests")


def test_generated_passwords_and_keys_satisfy_the_rule():
    for _ in range(50):
        pw = generate_password()
        check_password(pw)
        assert strong(pw)
        assert strong(generate_key_secret(32))


def test_generated_passwords_are_not_all_the_same():
    assert len({generate_password() for _ in range(20)}) == 20


def test_min_length_can_be_raised_but_never_lowered(monkeypatch):
    monkeypatch.setenv("RAMEN_MIN_PASSWORD_LEN", "20")
    assert min_length() == 20
    with pytest.raises(ApiError):
        check_password("Passw0rd!-19ch")
    monkeypatch.setenv("RAMEN_MIN_PASSWORD_LEN", "4")
    assert min_length() == MIN_LEN
    monkeypatch.setenv("RAMEN_MIN_PASSWORD_LEN", "not-a-number")
    assert min_length() == MIN_LEN


def test_generated_password_honours_a_raised_minimum(monkeypatch):
    monkeypatch.setenv("RAMEN_MIN_PASSWORD_LEN", "24")
    pw = generate_password()
    assert len(pw) >= 24
    check_password(pw)
