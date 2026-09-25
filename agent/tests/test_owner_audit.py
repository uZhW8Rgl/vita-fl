"""Real local HPKE/COSE primitives with synthetic keys; no cloud measurements."""

import hashlib

import cbor2
import pytest
from nacl.signing import SigningKey

from agent_receipts.owner_audit import CONTEXT_INFO, open_audit_context, seal_audit_context
from agent_receipts.sello_v1 import (
    HPKE_INFO,
    ReceiptVerificationError,
    SelloOwner,
    SelloReceiver,
    SelloVerifier,
    _canonical_cbor,
    _hpke_public,
    _suite,
    b64url_encode,
    verify_receipt_signature,
)


@pytest.fixture
def material():
    key = SigningKey.generate()
    owner = SelloOwner(
        SigningKey.generate(), b"o" * 32, {"tee": key.verify_key}, subject="owner", log_urls=["https://log.example"]
    )
    token = owner.token(
        audience="https://worker.example", pop_thumbprint=b64url_encode(b"p" * 32), scopes=["run"], now=1800000000
    )
    receiver = SelloReceiver("tee", key, owner.token_issuer_public_key)
    receipt = receiver.issue(
        token,
        action_type="run",
        action_input=b"exact\x00input",
        action_output=b"exact\xffoutput",
        result_status="success",
        now=1800000001,
    )
    context = seal_audit_context(
        receipt,
        owner.hpke_public_key,
        authorization_token=token,
        action_input=b"exact\x00input",
        action_output=b"exact\xffoutput",
    )
    return owner, receiver, token, receipt, context


def test_context_round_trip_does_not_publish_plaintext_or_private_keys(material):
    owner, _, token, receipt, context = material
    opened = open_audit_context(context, owner.hpke_private_key, hashlib.sha256(receipt).hexdigest())
    assert opened == {
        "authorization_token": token,
        "action_input": b"exact\x00input",
        "action_output": b"exact\xffoutput",
    }
    for secret in (
        token.encode(),
        b"exact\x00input",
        b"exact\xffoutput",
        owner.hpke_private_key,
        bytes(owner.token_signing_key),
    ):
        assert secret not in context
    assert CONTEXT_INFO != HPKE_INFO


@pytest.mark.parametrize(
    "failure", ["wrong_owner", "wrong_receipt", "edited_receipt_header", "ciphertext", "trailing", "unknown_version"]
)
def test_context_rejects_wrong_recipient_receipt_and_corrupt_encoding(material, failure):
    owner, _, _, receipt, raw = material
    key = owner.hpke_private_key
    digest = hashlib.sha256(receipt).hexdigest()
    value = cbor2.loads(raw)
    if failure == "wrong_owner":
        key = b"x" * 32
    elif failure == "wrong_receipt":
        digest = "f" * 64
    elif failure == "edited_receipt_header":
        digest = "f" * 64
        value[2] = bytes.fromhex(digest)
    elif failure == "ciphertext":
        value[4] = value[4][:-1] + bytes([value[4][-1] ^ 1])
    elif failure == "unknown_version":
        value[1] = 2
    raw = _canonical_cbor(value) + (b"\x00" if failure == "trailing" else b"")
    with pytest.raises(ReceiptVerificationError):
        open_audit_context(raw, key, digest)


def test_context_domain_prevents_receipt_suite_reuse(material):
    owner, _, _, receipt, _ = material
    digest = hashlib.sha256(receipt).digest()
    enc, sender = _suite().create_sender_context(_hpke_public(owner.hpke_public_key), info=HPKE_INFO)
    ciphertext = sender.seal(
        _canonical_cbor({1: "jwt", 2: b"i", 3: b"o"}), aad=_canonical_cbor([CONTEXT_INFO, 1, digest])
    )
    with pytest.raises(ReceiptVerificationError, match="decryption"):
        open_audit_context(
            _canonical_cbor({1: 1, 2: digest, 3: enc, 4: ciphertext}), owner.hpke_private_key, digest.hex()
        )


def test_read_only_verifier_requires_no_issuer_signing_seed(material):
    owner, receiver, token, receipt, _ = material
    verifier = SelloVerifier(owner.hpke_private_key, {"tee": receiver.public_key}, log_urls=owner.log_urls)
    assert not hasattr(verifier, "token_signing_key")
    verified = verifier.verify(
        receipt,
        token,
        expected_service="tee",
        expected_action="run",
        action_input=b"exact\x00input",
        action_output=b"exact\xffoutput",
    )
    assert verified.envelope_sha256 == hashlib.sha256(receipt).hexdigest()
    public = verify_receipt_signature(receipt, receiver.public_key, log_urls=owner.log_urls)
    assert public["token_ref"] == hashlib.sha256(token.encode()).digest()


@pytest.mark.parametrize("failure", ["receiver_key", "token_ref", "log", "signature"])
def test_public_signature_verification_rejects_mismatches(material, failure):
    owner, receiver, token, receipt, _ = material
    key, logs = receiver.public_key, owner.log_urls
    token_ref = hashlib.sha256(token.encode()).digest()
    if failure == "receiver_key":
        key = bytes(SigningKey.generate().verify_key)
    elif failure == "token_ref":
        token_ref = b"x" * 32
    elif failure == "log":
        logs = ["https://other.example"]
    else:
        value = list(cbor2.loads(receipt).value)
        value[3] = b"x" * 64
        receipt = _canonical_cbor(cbor2.CBORTag(18, value))
    with pytest.raises(ReceiptVerificationError):
        verify_receipt_signature(receipt, key, log_urls=logs, token_ref=token_ref)


def test_explicit_empty_receiver_key_does_not_fall_back_to_registry(material):
    owner, receiver, token, receipt, _ = material
    verifier = SelloVerifier(owner.hpke_private_key, {"tee": receiver.public_key}, log_urls=owner.log_urls)
    with pytest.raises(ValueError):
        verifier.verify(
            receipt,
            token,
            expected_service="tee",
            expected_action="run",
            action_input=b"exact\x00input",
            action_output=b"exact\xffoutput",
            trusted_service_key=b"",
        )
