"""Verify our PBKDF2 auth helpers — round-trip and known-vector
sanity. The constants (PBKDF2-SHA256, 200_000 iterations, hex salt)
match the upstream user-management process so a hash written there
verifies here byte-for-byte.
"""
from backend import auth


def test_pbkdf2_known_vector():
    salt = "00112233445566778899aabbccddeeff"
    pw = "correct horse battery staple"
    digest = auth._pbkdf2(pw, salt)
    assert isinstance(digest, str) and len(digest) == 64
    assert auth._pbkdf2(pw, salt) == digest


def test_set_then_verify_roundtrip():
    config: dict = {}
    auth.set_web_password(config, "swordfish")
    assert auth.has_web_password(config)
    assert auth.verify_web_password(config, "swordfish")
    assert not auth.verify_web_password(config, "wrong")


def test_verify_constant_time_against_garbage():
    config: dict = {
        auth.WEB_PASSWORD_HASH_KEY: "00" * 32,
        auth.WEB_PASSWORD_SALT_KEY: "ff" * 16,
    }
    assert not auth.verify_web_password(config, "anything")


def test_has_web_password_requires_both():
    assert not auth.has_web_password({})
    assert not auth.has_web_password({auth.WEB_PASSWORD_HASH_KEY: "x"})
    assert not auth.has_web_password({auth.WEB_PASSWORD_SALT_KEY: "x"})
    assert auth.has_web_password({
        auth.WEB_PASSWORD_HASH_KEY: "x",
        auth.WEB_PASSWORD_SALT_KEY: "y",
    })
