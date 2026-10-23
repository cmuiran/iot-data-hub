"""Application-level encryption with HUB_DATA_KEY: TOTP secrets and backup files.

* :func:`seal` / :func:`unseal`: small secrets (AES-256-GCM, random nonce), stored as text
  ``v1.<base64>``.
* :func:`encrypt_file` / :func:`decrypt_file`: large files in 1 MiB chunks, each sealed with its
  own nonce and authenticated together with its index and a final-chunk flag, so chunks cannot
  be reordered, dropped or truncated unnoticed. Format: ``HUBENC1`` + chunks of
  ``[nonce 12][length 4][ciphertext+tag]``.

The key is derived from HUB_DATA_KEY with HKDF-SHA256 (separate sub-keys per purpose). Losing
HUB_DATA_KEY means losing encrypted backups and MFA enrolments; keep it in a password safe.
"""

from __future__ import annotations

import base64
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"HUBENC1"
CHUNK = 1024 * 1024


class CryptoError(Exception):
    pass


def derive_key(data_key: str, purpose: str) -> bytes:
    if not data_key or len(data_key) < 32:
        raise CryptoError("HUB_DATA_KEY must be at least 32 characters")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"iot-data-hub",
                info=f"hub:{purpose}:v1".encode()).derive(data_key.encode())


def seal(data_key: str, plaintext: str, purpose: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(derive_key(data_key, purpose)).encrypt(nonce, plaintext.encode(),
                                                      purpose.encode())
    return "v1." + base64.urlsafe_b64encode(nonce + ct).decode()


def unseal(data_key: str, token: str, purpose: str) -> str:
    if not token.startswith("v1."):
        raise CryptoError("unknown secret format")
    raw = base64.urlsafe_b64decode(token[3:].encode())
    try:
        return AESGCM(derive_key(data_key, purpose)).decrypt(raw[:12], raw[12:],
                                                             purpose.encode()).decode()
    except InvalidTag as exc:
        raise CryptoError("cannot decrypt: wrong HUB_DATA_KEY or damaged data") from exc


def _aad(index: int, final: bool) -> bytes:
    return MAGIC + struct.pack(">Q?", index, final)


def encrypt_file(data_key: str, src: Path, dst: Path) -> None:
    aes = AESGCM(derive_key(data_key, "backup"))
    size = src.stat().st_size
    with src.open("rb") as fin, dst.open("wb") as fout:
        fout.write(MAGIC)
        index, done = 0, 0
        while True:
            block = fin.read(CHUNK)
            done += len(block)
            final = done >= size
            nonce = os.urandom(12)
            ct = aes.encrypt(nonce, block, _aad(index, final))
            fout.write(nonce + struct.pack(">I", len(ct)) + ct)
            index += 1
            if final:
                break


def decrypt_file(data_key: str, src: Path, dst: Path) -> None:
    aes = AESGCM(derive_key(data_key, "backup"))
    with src.open("rb") as fin, dst.open("wb") as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise CryptoError("not an encrypted hub backup")
        index = 0
        while True:
            head = fin.read(16)
            if len(head) < 16:
                raise CryptoError("file is truncated (final chunk missing)")
            nonce, (length,) = head[:12], struct.unpack(">I", head[12:])
            ct = fin.read(length)
            if len(ct) < length:
                raise CryptoError("file is truncated inside a chunk")
            final = fin.peek(1) == b""  # type: ignore[attr-defined]
            try:
                block = aes.decrypt(nonce, ct, _aad(index, final))
            except InvalidTag:
                raise CryptoError("wrong HUB_DATA_KEY, or the backup is damaged, truncated or "
                                  "reordered") from None
            fout.write(block)
            index += 1
            if final:
                return
