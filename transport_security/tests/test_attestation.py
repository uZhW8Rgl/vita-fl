"""Hostile evidence tests; the remote hardware verifier is an explicit HTTP mock."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock

import cbor2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from transport_security import attestation as a


def certificate():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tee.example")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER), cert.public_bytes(serialization.Encoding.PEM)


def quote_for(report_data, *, debug=False):
    quote = bytearray(700)
    quote[:2] = (4).to_bytes(2, "little")
    quote[2:4] = (2).to_bytes(2, "little")
    quote[4:8] = (0x81).to_bytes(4, "little")
    quote[168:176] = int(debug).to_bytes(8, "little")
    for index, offset in enumerate(a.TDX_MEASUREMENT_OFFSETS.values(), 1):
        quote[48 + offset : 48 + offset + 48] = bytes([index]) * 48
    quote[568:632] = report_data
    quote[632:636] = (64).to_bytes(4, "little")
    return bytes(quote)


def api_value(quote):
    return {
        "success": True,
        "checksum": hashlib.sha256(quote).hexdigest(),
        "quote": {"verified": True, "body": {name: value.hex() for name, value in a.parse_tdx_quote(quote).items()}},
    }


def test_quote_parser_accepts_only_zero_padding_after_signature():
    quote = quote_for(b"r" * 64)
    assert a.parse_tdx_quote(quote + bytes(70)) == a.parse_tdx_quote(quote)


@pytest.mark.parametrize(
    "quote",
    [
        quote_for(b"r" * 64)[:-1],
        quote_for(b"r" * 64) + bytes(69) + b"\x01",
        quote_for(b"r" * 64)[:632] + bytes(4) + quote_for(b"r" * 64)[636:],
    ],
    ids=["truncated-signature", "nonzero-trailing-data", "empty-signature"],
)
def test_quote_parser_rejects_invalid_signature_boundaries(quote):
    with pytest.raises(a.AttestationVerificationError):
        a.parse_tdx_quote(quote)


class ApiResponse:
    status = 200

    def __init__(self, value, content_type="application/json", status=200):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.raw[:limit]


@pytest.fixture
def fixture(monkeypatch):
    der, pem = certificate()
    claims = {
        "version": 1,
        "challenge": b"c" * 32,
        "tls_spki_sha256": a.tls_spki_sha256(der),
        "air_public_key": b"a" * 32,
        "sello_public_key": b"s" * 32,
        "origin": "https://tee.example",
        "issued_at": 1000,
        "expires_at": 1300,
    }
    quote = quote_for(a.session_report_data(claims))
    measurements = a.parse_tdx_quote(quote)
    monkeypatch.setenv(
        "RATLS_ALLOWED_PLATFORM_MEASUREMENTS",
        json.dumps([{name: measurements[name].hex() for name in ("mrtd", "rtmr0", "rtmr1", "rtmr2")}]),
    )
    monkeypatch.delenv("PHALA_ATTESTATION_VERIFY_URL", raising=False)
    monkeypatch.delenv("PHALA_ATTESTATION_ALLOWED_HOSTS", raising=False)
    evidence = a.encode_session_evidence(claims, quote, "[]", "{}")
    opener = Mock()
    opener.open.return_value = ApiResponse(api_value(quote))
    raw_by_checksum = {api_value(quote)["checksum"]: ApiResponse(quote, "application/octet-stream")}

    def api_open(request, timeout):
        if request.get_method() == "POST":
            return opener.open.return_value
        checksum = request.full_url.rsplit("/", 1)[-1]
        if checksum not in raw_by_checksum:
            raise a.urllib.error.HTTPError(request.full_url, 404, "Not found", {}, None)
        response = raw_by_checksum[checksum]
        if isinstance(response, Exception):
            raise response
        return response

    opener.open.side_effect = api_open
    monkeypatch.setattr(a.urllib.request, "build_opener", lambda *args: opener)
    return SimpleNamespace(
        der=der, pem=pem, claims=claims, quote=quote, evidence=evidence, opener=opener, raw_by_checksum=raw_by_checksum
    )


def verify(fixture, **changes):
    args = {
        "evidence": fixture.evidence,
        "challenge": fixture.claims["challenge"],
        "peer_cert_der": fixture.der,
        "expected_origin": fixture.claims["origin"],
        "expected_sello_key": fixture.claims["sello_public_key"],
        "deployment_validator": lambda event_log, compose, quote: None,
        "now": 1010,
    }
    args.update(changes)
    return a.verify_session_evidence(**args)


def test_session_binds_exact_peer_key_challenge_sello_and_quote(fixture):
    validator = Mock()
    session = verify(fixture, deployment_validator=validator)
    assert session.session_id == hashlib.sha256(fixture.evidence).hexdigest()
    assert session.evidence == fixture.evidence
    assert session.quote_verification["verified"] is True
    validator.assert_called_once_with("[]", "{}", fixture.quote)
    post, raw_get = fixture.opener.open.call_args_list
    assert post.args[0].full_url == a.DEFAULT_PHALA_VERIFY_URL
    assert json.loads(post.args[0].data) == {"hex": fixture.quote.hex()}
    assert 0 < raw_get.kwargs["timeout"] <= post.kwargs["timeout"] <= 20
    assert post.args[0].get_header("User-agent") == "vita-fl-attestation/1"
    assert raw_get.args[0].get_method() == "GET"
    assert (
        raw_get.args[0].full_url
        == a.DEFAULT_PHALA_VERIFY_URL.replace("/verify", "/raw/") + api_value(fixture.quote)["checksum"]
    )
    assert raw_get.args[0].get_header("Accept") == "application/octet-stream"
    assert raw_get.args[0].get_header("User-agent") == "vita-fl-attestation/1"
    with pytest.raises(TypeError):
        session.claims["challenge"] = b"x" * 32


def test_zero_padded_quote_is_preserved_in_evidence_and_verifier_request(fixture):
    quote = fixture.quote + bytes(70)
    evidence = a.encode_session_evidence(fixture.claims, quote, "[]", "{}")
    fixture.opener.open.return_value = ApiResponse(api_value(quote))
    fixture.raw_by_checksum[api_value(quote)["checksum"]] = ApiResponse(quote, "application/octet-stream")
    session = verify(fixture, evidence=evidence)
    assert session.evidence == evidence
    assert a.decode_session_evidence(session.evidence)["quote"] == quote
    assert json.loads(fixture.opener.open.call_args_list[0].args[0].data) == {"hex": quote.hex()}
    assert session.quote_verification["verified"] is True


@pytest.mark.parametrize(
    "change,match",
    [
        ({"challenge": b"x" * 32}, "challenge"),
        ({"expected_sello_key": b"x" * 32}, "Sello"),
        ({"expected_origin": "https://another.example"}, "origin"),
        ({"now": 1300}, "stale"),
        ({"now": 969}, "future"),
        ({"deployment_validator": None}, "deployment"),
    ],
)
def test_session_rejects_wrong_context_before_network(fixture, change, match):
    with pytest.raises(a.AttestationVerificationError, match=match):
        verify(fixture, **change)
    fixture.opener.open.assert_not_called()


def test_different_tls_peer_key_is_rejected(fixture):
    other_der, _ = certificate()
    with pytest.raises(a.AttestationVerificationError, match="TLS key"):
        verify(fixture, peer_cert_der=other_der)
    fixture.opener.open.assert_not_called()


def test_edited_claims_cannot_reuse_genuine_quote(fixture):
    value = cbor2.loads(fixture.evidence)
    value["claims"]["air_public_key"] = b"x" * 32
    with pytest.raises(a.AttestationVerificationError, match="REPORTDATA"):
        verify(fixture, evidence=a.canonical(value))
    fixture.opener.open.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(success=False),
        lambda value: value.update(success=1),
        lambda value: value["quote"].update(verified=False),
        lambda value: value["quote"].update(verified="true"),
        lambda value: value.update(checksum="ff" * 32),
        lambda value: value["quote"]["body"].update(reportdata="ff" * 64),
        lambda value: value["quote"]["body"].pop("mrtd"),
    ],
)
def test_untrusted_api_verdict_or_mismatched_quote_is_rejected(fixture, mutation):
    value = api_value(fixture.quote)
    mutation(value)
    fixture.opener.open.return_value = ApiResponse(value)
    with pytest.raises(a.AttestationVerificationError):
        verify(fixture)


def test_provider_identifier_can_differ_from_local_quote_hash(fixture):
    value = api_value(fixture.quote)
    value["checksum"] = "0x" + "AB" * 32
    fixture.opener.open.return_value = ApiResponse(value)
    fixture.raw_by_checksum["ab" * 32] = ApiResponse(fixture.quote, "application/octet-stream")
    session = verify(fixture)
    assert session.quote_verification["provider_checksum"] == "ab" * 32
    assert session.quote_verification["quote_sha256"] == hashlib.sha256(fixture.quote).hexdigest()
    assert fixture.opener.open.call_args.args[0].full_url.endswith("/raw/" + "ab" * 32)


@pytest.mark.parametrize("checksum", [None, "", "ab" * 31, "ab" * 33, "gg" * 32, "../quote", "https://other.test/"])
def test_malformed_provider_identifier_is_rejected_before_raw_request(fixture, checksum):
    value = api_value(fixture.quote)
    value["checksum"] = checksum
    fixture.opener.open.return_value = ApiResponse(value)
    with pytest.raises(a.AttestationVerificationError, match="checksum"):
        verify(fixture)
    assert fixture.opener.open.call_count == 1


def test_different_provider_identifier_cannot_substitute_other_quote(fixture):
    value = api_value(fixture.quote)
    value["checksum"] = "ff" * 32
    fixture.opener.open.return_value = ApiResponse(value)
    other_quote = quote_for(b"other REPORTDATA".ljust(64, b"\x00"))
    fixture.raw_by_checksum[value["checksum"]] = ApiResponse(other_quote, "application/octet-stream")
    with pytest.raises(a.AttestationVerificationError, match="raw quote differs"):
        verify(fixture)


@pytest.mark.parametrize("fault", ["different", "truncated", "oversized", "nonbinary", "http", "network", "redirect"])
def test_raw_quote_lookup_failures_are_rejected(fixture, fault):
    checksum = api_value(fixture.quote)["checksum"]
    raw_url = a.DEFAULT_PHALA_VERIFY_URL.replace("/verify", "/raw/") + checksum
    cases = {
        "different": ApiResponse(bytes(len(fixture.quote)), "application/octet-stream"),
        "truncated": ApiResponse(fixture.quote[:-1], "application/octet-stream"),
        "oversized": ApiResponse(bytes(a.MAX_QUOTE_BYTES + 1), "application/octet-stream"),
        "nonbinary": ApiResponse(fixture.quote, "text/plain"),
        "http": a.urllib.error.HTTPError(raw_url, 503, "private server response", {}, None),
        "network": a.urllib.error.URLError("private network response"),
        "redirect": ApiResponse(fixture.quote, "application/octet-stream", status=302),
    }
    fixture.raw_by_checksum[checksum] = cases[fault]
    with pytest.raises(a.AttestationVerificationError) as caught:
        verify(fixture)
    assert fixture.opener.open.call_count == 2
    assert "private" not in str(caught.value)


def test_padding_is_part_of_exact_raw_quote_binding(fixture):
    quote = fixture.quote + bytes(70)
    fixture.opener.open.return_value = ApiResponse(api_value(quote))
    fixture.raw_by_checksum[api_value(quote)["checksum"]] = ApiResponse(fixture.quote, "application/octet-stream")
    with pytest.raises(a.AttestationVerificationError, match="raw quote differs"):
        verify(fixture, evidence=a.encode_session_evidence(fixture.claims, quote, "[]", "{}"))


def test_post_and_raw_lookup_share_the_timeout_budget(fixture, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(a.time, "monotonic", lambda: clock.now)
    original_open = fixture.opener.open.side_effect

    def timed_open(request, timeout):
        clock.now += 8 if request.get_method() == "POST" else 2
        return original_open(request, timeout)

    fixture.opener.open.side_effect = timed_open
    verify(fixture)
    post, raw_get = fixture.opener.open.call_args_list
    assert post.kwargs["timeout"] == 20
    assert raw_get.kwargs["timeout"] == 12


@pytest.mark.parametrize("slow_method", ["POST", "GET"])
def test_expired_total_budget_prevents_acceptance(fixture, monkeypatch, slow_method):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(a.time, "monotonic", lambda: clock.now)
    original_open = fixture.opener.open.side_effect

    def timed_open(request, timeout):
        clock.now += 21 if request.get_method() == slow_method else 1
        return original_open(request, timeout)

    fixture.opener.open.side_effect = timed_open
    with pytest.raises(a.AttestationVerificationError, match="deadline exceeded"):
        verify(fixture)
    assert fixture.opener.open.call_count == (1 if slow_method == "POST" else 2)


def test_api_duplicate_fields_and_oversized_response_rejected(fixture):
    for raw in (b'{"success":false,"success":true}', b" " * (a.MAX_API_RESPONSE_BYTES + 1)):
        fixture.opener.open.return_value = ApiResponse(raw)
        with pytest.raises(a.AttestationVerificationError):
            verify(fixture)


def test_api_network_failure_fails_closed(fixture):
    fixture.opener.open.side_effect = TimeoutError("timed out")
    with pytest.raises(a.AttestationVerificationError, match="unavailable"):
        verify(fixture)


def test_api_http_failure_reports_status_without_remote_body(fixture):
    error = a.urllib.error.HTTPError(a.DEFAULT_PHALA_VERIFY_URL, 403, "private response", {}, None)
    fixture.opener.open.side_effect = error
    with pytest.raises(a.AttestationVerificationError) as caught:
        verify(fixture)
    assert str(caught.value) == "Phala quote verification is unavailable (HTTP 403)"


@pytest.mark.parametrize(
    "url",
    [
        "http://cloud-api.phala.com/api/v1/attestations/verify",
        "https://attacker.example/api/v1/attestations/verify",
        "https://cloud-api.phala.com:444/api/v1/attestations/verify",
        "https://cloud-api.phala.com/api/v1/attestations/verify?redirect=1",
    ],
)
def test_verifier_destination_cannot_be_redirected_by_evidence(fixture, monkeypatch, url):
    monkeypatch.setenv("PHALA_ATTESTATION_VERIFY_URL", url)
    with pytest.raises(a.AttestationVerificationError, match="allowlist"):
        verify(fixture)
    fixture.opener.open.assert_not_called()


def test_redirect_handler_never_follows(fixture):
    with pytest.raises(a.AttestationVerificationError, match="redirect"):
        a._NoRedirect().redirect_request(None, None, 307, "Redirect", {}, "https://attacker.example")


def test_platform_policy_and_debug_are_enforced_before_api(fixture, monkeypatch):
    monkeypatch.delenv("RATLS_ALLOWED_PLATFORM_MEASUREMENTS")
    with pytest.raises(a.AttestationVerificationError, match="allowlist"):
        verify(fixture)
    quote = quote_for(a.session_report_data(fixture.claims), debug=True)
    with pytest.raises(a.AttestationVerificationError, match="debug"):
        a.verify_quote_with_phala(quote, a.session_report_data(fixture.claims))
    fixture.opener.open.assert_not_called()


def test_platform_tuple_must_match_as_a_whole(fixture, monkeypatch):
    fields = a.parse_tdx_quote(fixture.quote)
    first = {name: fields[name].hex() for name in ("mrtd", "rtmr0", "rtmr1", "rtmr2")}
    second = dict(first)
    first["mrtd"] = "ff" * 48
    second["rtmr1"] = "ff" * 48
    monkeypatch.setenv("RATLS_ALLOWED_PLATFORM_MEASUREMENTS", json.dumps([first, second]))
    with pytest.raises(a.AttestationVerificationError, match="platform allowlist"):
        verify(fixture)
    fixture.opener.open.assert_not_called()


def test_bad_canonical_evidence_is_rejected(fixture):
    with pytest.raises(a.AttestationVerificationError):
        verify(fixture, evidence=fixture.evidence + b"\x00")
    value = cbor2.loads(fixture.evidence)
    value["claims"]["expires_at"] = 1301
    with pytest.raises(a.AttestationVerificationError, match="lifetime"):
        verify(fixture, evidence=a.canonical(value))


def test_deployment_policy_failure_is_not_swallowed(fixture):
    def rejected(*args):
        raise a.AttestationVerificationError("wrong image")

    with pytest.raises(a.AttestationVerificationError, match="wrong image"):
        verify(fixture, deployment_validator=rejected)
