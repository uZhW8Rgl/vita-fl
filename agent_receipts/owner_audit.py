"""Owner-only context for a later independent Sello receipt appraisal.

Only the HPKE envelope is published. Its domain and authenticated data are
different from the receipt itself, and bind the exact signed receipt digest.
This does not publish a signing seed, HPKE private key, or plaintext JWT/I/O.
"""

from __future__ import annotations

import hashlib
import re

from .sello_v1 import (
    ReceiptVerificationError,
    _canonical_cbor,
    _hpke_private,
    _hpke_public,
    _loads_canonical_cbor,
    _suite,
)

CONTEXT_CONTENT_TYPE = "application/vnd.master-thesis.sello-verification-context+cbor"
CONTEXT_INFO = b"master-thesis/sello-v1/owner-verification-context/v1"


def _digest(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReceiptVerificationError("audit context requires a receipt SHA256")
    return bytes.fromhex(value)


def _aad(digest: bytes) -> bytes:
    return _canonical_cbor([CONTEXT_INFO, 1, digest])


def seal_audit_context(
    receipt: bytes,
    owner_public_key: bytes,
    *,
    authorization_token: str,
    action_input: bytes,
    action_output: bytes,
) -> bytes:
    """Encrypt exact original authorization/request/response bytes to the owner."""
    if not isinstance(authorization_token, str) or not authorization_token:
        raise ReceiptVerificationError("audit context requires the original authorization token")
    if not isinstance(action_input, bytes) or not isinstance(action_output, bytes):
        raise ReceiptVerificationError("audit context requires exact request and response bytes")
    digest = hashlib.sha256(receipt).digest()
    plaintext = _canonical_cbor({1: authorization_token, 2: action_input, 3: action_output})
    enc, sender = _suite().create_sender_context(_hpke_public(owner_public_key), info=CONTEXT_INFO)
    ciphertext = sender.seal(plaintext, aad=_aad(digest))
    return _canonical_cbor({1: 1, 2: digest, 3: enc, 4: ciphertext})


def open_audit_context(raw: bytes, owner_private_key: bytes, expected_receipt_sha256: str) -> dict:
    """Decrypt for authorized local appraisal; callers must never persist this result."""
    digest = _digest(expected_receipt_sha256)
    value = _loads_canonical_cbor(raw, "owner audit context")
    if (
        not isinstance(value, dict)
        or set(value) != {1, 2, 3, 4}
        or type(value[1]) is not int
        or value[1] != 1
        or value[2] != digest
        or not isinstance(value[3], bytes)
        or len(value[3]) != 32
        or not isinstance(value[4], bytes)
    ):
        raise ReceiptVerificationError("owner audit context profile or receipt binding is invalid")
    try:
        recipient = _suite().create_recipient_context(value[3], _hpke_private(owner_private_key), info=CONTEXT_INFO)
        plaintext = recipient.open(value[4], aad=_aad(digest))
    except Exception as exc:
        raise ReceiptVerificationError("owner audit context decryption failed") from exc
    context = _loads_canonical_cbor(plaintext, "owner audit plaintext")
    if (
        not isinstance(context, dict)
        or set(context) != {1, 2, 3}
        or not isinstance(context[1], str)
        or not context[1]
        or not all(isinstance(context[key], bytes) for key in (2, 3))
    ):
        raise ReceiptVerificationError("owner audit plaintext profile is invalid")
    return {"authorization_token": context[1], "action_input": context[2], "action_output": context[3]}
