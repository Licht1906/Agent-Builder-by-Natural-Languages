"""UC1-01 (#117), FR-02: Argon2id for new hashes, bcrypt still verifiable for legacy ones."""

import bcrypt

from src.auth.passwords import DUMMY_HASH, hash_password, verify_password


def test_hash_password_produces_an_argon2id_hash() -> None:
    assert hash_password("a-real-password-123").startswith("$argon2id$")


def test_a_freshly_hashed_password_verifies() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_the_wrong_password_does_not_verify() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong password entirely", hashed) is False


def test_a_legacy_bcrypt_hash_still_verifies() -> None:
    """Accounts seeded/registered before UC1-01 hold bcrypt hashes — they must keep logging in."""
    legacy_hash = bcrypt.hashpw(b"old-account-password", bcrypt.gensalt()).decode("utf-8")

    assert verify_password("old-account-password", legacy_hash) is True
    assert verify_password("wrong-guess", legacy_hash) is False


def test_an_unparseable_hash_fails_closed_rather_than_raising() -> None:
    assert verify_password("anything", "not-a-real-hash-at-all") is False


def test_dummy_hash_never_matches_a_real_password() -> None:
    assert verify_password("no-account-with-this-hash", DUMMY_HASH) is True
    assert verify_password("something-else", DUMMY_HASH) is False
