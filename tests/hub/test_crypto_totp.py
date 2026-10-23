from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend import totp
from backend.crypto import CryptoError, decrypt_file, encrypt_file, seal, unseal

KEY = "k" * 40


def test_seal_round_trip_and_purpose_binding() -> None:
    token = seal(KEY, "JBSWY3DPEHPK3PXP", "totp")
    assert token.startswith("v1.") and "JBSWY3DP" not in token
    assert unseal(KEY, token, "totp") == "JBSWY3DPEHPK3PXP"
    with pytest.raises(CryptoError):
        unseal("x" * 40, token, "totp")           # wrong key
    with pytest.raises(CryptoError):
        unseal(KEY, token, "backup")               # sealed for another purpose
    with pytest.raises(CryptoError, match="at least 32"):
        seal("short", "x", "totp")


@pytest.mark.parametrize("size", [0, 10, 1024 * 1024, 1024 * 1024 * 2 + 17])
def test_file_encryption_round_trip(tmp_path: Path, size: int) -> None:
    src, enc, out = tmp_path / "a.dump", tmp_path / "a.enc", tmp_path / "b.dump"
    data = os.urandom(size)
    src.write_bytes(data)
    encrypt_file(KEY, src, enc)
    assert data[:64] not in enc.read_bytes() or size == 0
    decrypt_file(KEY, enc, out)
    assert out.read_bytes() == data


def test_truncated_or_tampered_backup_is_detected(tmp_path: Path) -> None:
    src, enc, out = tmp_path / "a", tmp_path / "a.enc", tmp_path / "b"
    src.write_bytes(os.urandom(1024 * 1024 * 2 + 5))
    encrypt_file(KEY, src, enc)
    blob = enc.read_bytes()
    chunk = 7 + 16 + 1024 * 1024 + 16
    (tmp_path / "cut.enc").write_bytes(blob[:7 + chunk])          # drop the last chunks
    with pytest.raises(CryptoError):
        decrypt_file(KEY, tmp_path / "cut.enc", out)
    flipped = bytearray(blob)
    flipped[100] ^= 1
    (tmp_path / "bad.enc").write_bytes(bytes(flipped))
    with pytest.raises(CryptoError):
        decrypt_file(KEY, tmp_path / "bad.enc", out)
    with pytest.raises(CryptoError):
        decrypt_file("z" * 40, enc, out)


def test_totp_rfc6238_vector() -> None:
    # RFC 6238 appendix B, SHA1 key "12345678901234567890" at T=59 -> 94287082 (8 digits)
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(secret, 59 // 30) == "287082"


def test_totp_window_and_replay() -> None:
    secret = totp.new_secret()
    now = 1_767_225_600.0
    code = totp.code_at(secret, totp.step_of(now))
    step = totp.verify(secret, code, now, None)
    assert step == totp.step_of(now)
    assert totp.verify(secret, code, now, last_step=step) is None          # no replay
    previous = totp.code_at(secret, totp.step_of(now) - 1)
    assert totp.verify(secret, previous, now, None) is not None            # 30 s drift ok
    old = totp.code_at(secret, totp.step_of(now) - 3)
    assert totp.verify(secret, old, now, None) is None
    assert totp.verify(secret, "12345", now, None) is None


def test_provisioning_uri_and_recovery_codes() -> None:
    uri = totp.provisioning_uri("ABC", "op.one")
    assert uri.startswith("otpauth://totp/IoT%20Data%20Hub%3Aop.one?secret=ABC")
    codes = totp.new_recovery_codes()
    assert len(set(codes)) == 10 and all(len(c) == 9 and c[4] == "-" for c in codes)
    assert totp.hash_recovery_code(codes[0].upper()) == totp.hash_recovery_code(codes[0])
