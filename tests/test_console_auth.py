"""Console login: password hashing, signed sessions, isolation between projects, throttling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui" / "backend"))

from auth import MAX_FAILURES, Auth, hash_password, verify_password  # noqa: E402

SECRET_A, SECRET_B = "a" * 48, "b" * 48


@pytest.fixture(scope="module")
def alice_hash():
    return hash_password("correct horse battery")


def _auth(alice_hash, project="credit-risk", secret=SECRET_A, **extra):
    return Auth({"CONSOLE_USERS": f"alice:{alice_hash}", "CONSOLE_SESSION_SECRET": secret,
                 "CONSOLE_PROJECT": project, **extra})


def test_password_hash_round_trip(alice_hash):
    assert verify_password("correct horse battery", alice_hash)
    assert not verify_password("wrong", alice_hash)
    assert not verify_password("correct horse battery", "md5$1$x$y")


def test_login_and_session(alice_hash):
    auth = _auth(alice_hash)
    assert auth.check("1.2.3.4", "Alice", "wrong") is None
    user = auth.check("1.2.3.4", "Alice", "correct horse battery")
    assert user == "alice"
    assert auth.user_for(auth.issue(user)) == "alice"


def test_session_from_another_project_is_rejected(alice_hash):
    """Same username in two deployments: neither accepts the other's cookie."""
    credit = _auth(alice_hash, project="credit-risk", secret=SECRET_A)
    other = _auth(alice_hash, project="other-project", secret=SECRET_B)
    assert other.user_for(credit.issue("alice")) is None
    assert credit.user_for(other.issue("alice")) is None
    assert credit.cookie != other.cookie


def test_same_secret_different_project_is_rejected(alice_hash):
    credit = _auth(alice_hash, project="credit-risk")
    other = _auth(alice_hash, project="other-project")
    assert other.user_for(credit.issue("alice")) is None


def test_tampered_expired_and_removed_user_sessions_are_rejected(alice_hash):
    auth = _auth(alice_hash)
    token = auth.issue("alice")
    payload, sig = token.rsplit(".", 1)
    assert auth.user_for(payload + "." + sig[:-2] + "xx") is None
    assert auth.user_for(auth.issue("alice", now=0)) is None
    assert auth.user_for(auth.issue("mallory")) is None


def test_failed_logins_are_throttled_per_client(alice_hash):
    auth = _auth(alice_hash)
    for _ in range(MAX_FAILURES):
        auth.check("9.9.9.9", "alice", "nope")
    assert auth.throttled("9.9.9.9")
    assert not auth.throttled("8.8.8.8")


def test_configuration_fails_closed(alice_hash):
    with pytest.raises(ValueError):
        Auth({"CONSOLE_USERS": f"alice:{alice_hash}", "CONSOLE_SESSION_SECRET": "short"})
    with pytest.raises(ValueError):
        Auth({"CONSOLE_AUTH_REQUIRED": "1"})
    assert not Auth({}).enabled
