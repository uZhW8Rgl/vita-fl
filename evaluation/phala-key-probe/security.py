"""Cloud component experiments: production cryptography, sessions and model IO.

No private key, authorization token or plaintext model is returned. HTTP/TLS
deployment integration and production disk forensics are separate boundaries.
"""
import base64
import contextlib
import hashlib
import json
import io
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from nacl.signing import SigningKey


def keys(challenge):
    from agent_receipts.dstack_key import tee_sello_signing_seed
    from tee_inference.service.attestation import derive_air_signing_key

    sello = SigningKey(tee_sello_signing_seed())
    # The production ephemeral loader reads the runtime seed; it makes no RPC.
    air = derive_air_signing_key(None)
    message = bytes.fromhex(challenge)
    for key in (sello, air):
        key.verify_key.verify(message, key.sign(message).signature)
    return {
        "sello_public_key_sha256": hashlib.sha256(bytes(sello.verify_key)).hexdigest(),
        "air_public_key_sha256": hashlib.sha256(bytes(air.verify_key)).hexdigest(),
        "signatures_verified": True,
    }


def security(challenge):
    from agent_receipts.sello_v1 import create_authorization_token, verify_authorization_token, ReceiptVerificationError
    from transport_security.pop import (PoPIdentity, ReplayCache, proof_headers,
                                        public_key_thumbprint, verify_pop_proof, PoPAuthenticationError)
    from transport_security.attestation import AttestationVerificationError, SessionBinding
    from tee_inference.service.ratls import RatlsSessionManager
    from agent_receipts.dstack_key import tee_sello_signing_seed
    from tee_inference.service.attestation import DstackClient
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from datetime import datetime, timedelta, timezone

    checks=[]
    def check(name,actual,expected=True):
        checks.append({"name":name,"passed":actual==expected,"actual":actual,"expected":expected})
    def rejected(name,operation,exception,reason):
        try:
            operation()
        except exception as error:
            check(name,reason in str(error))
        else:
            check(name,False)
    now=int(time.time())
    issuer=SigningKey.generate();agent=SigningKey.generate()
    identity=PoPIdentity("chapter7-agent",bytes(agent),public_key_thumbprint(bytes(agent.verify_key)))
    origin="https://chapter7-component.example"
    token=create_authorization_token(issuer,secrets.token_bytes(32),subject=identity.subject,
        log_urls=["https://chapter7-log.example"],audience=origin,pop_thumbprint=identity.thumbprint,
        scopes=["model-fetch"],now=now)
    request={"method":"POST","url":origin+"/v1/models/fetch","body":b'{}',"token":token,
             "session_id":"chapter7-session-"+secrets.token_hex(16)}
    headers=proof_headers(identity,**request,now=now)
    def verify(proof=headers,cache=None,at=now,**changes):
        return verify_pop_proof(proof,**{**request,**changes},registry={identity.subject:identity.public_key},
                                replay_cache=cache if cache is not None else ReplayCache(),now=at)
    cache=ReplayCache();check("valid_pop",verify(cache=cache).subject==identity.subject)
    rejected("same_proof_twice",lambda:verify(cache=cache),PoPAuthenticationError,"replayed")
    cache=ReplayCache()
    def concurrent(_):
        try: verify(cache=cache);return True
        except PoPAuthenticationError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        check("concurrent_same_proof",sum(pool.map(concurrent,range(16))),1)
    for name,change,reason in [
        ("changed_body",{"body":b'{"different":true}'},"body_sha256 binding"),
        ("changed_method",{"method":"GET"},"htm binding"),
        ("changed_url",{"url":origin+"/v1/jobs"},"htu binding"),
        ("changed_token",{"token":token+"x"},"ath binding"),
        ("changed_session",{"session_id":"another-session-"+secrets.token_hex(16)},"sid binding"),
    ]:
        rejected(name,lambda change=change:verify(**change),PoPAuthenticationError,reason)
    old=proof_headers(identity,**request,now=now-61)
    rejected("expired_pop",lambda:verify(old),PoPAuthenticationError,"validity window")
    check("valid_token",verify_authorization_token(token,issuer.verify_key,now=now,
        expected_audience=origin,required_scope="model-fetch",expected_pop_thumbprint=identity.thumbprint)["sub"]==identity.subject)
    rejected("expired_token",lambda:verify_authorization_token(token,issuer.verify_key,now=now+1000),
             ReceiptVerificationError,"validity")

    # Use the actual session issuer and dstack quote, without fake session insertion.
    certificate=Path('/run/vita-fl/evaluation-session-cert.pem')
    certificate_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME,"chapter7-component")])
    current=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
          .public_key(certificate_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(current-timedelta(minutes=1)).not_valid_after(current+timedelta(hours=1))
          .sign(certificate_key,hashes.SHA256()))
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    sello_public=bytes(SigningKey(tee_sello_signing_seed()).verify_key)
    clock=[now]
    manager=RatlsSessionManager(sello_public,origin,client=DstackClient(),certificate_path=certificate,
                                clock=lambda:clock[0])
    evidence=manager.create_session(bytes.fromhex(challenge));binding=SessionBinding.from_evidence(evidence)
    check("valid_real_quote_session",manager.require_session(binding.session_id).session_id==binding.session_id)
    restarted=RatlsSessionManager(sello_public,origin,client=DstackClient(),certificate_path=certificate,
                                  clock=lambda:clock[0])
    rejected("restart_old_session",lambda:restarted.require_session(binding.session_id),
             AttestationVerificationError,"unknown or expired")
    clock[0]=binding.claims["expires_at"]
    rejected("expired_session",lambda:manager.require_session(binding.session_id),
             AttestationVerificationError,"unknown or expired")

    # Exercise the production receive/decrypt/write path with a random test model.
    from dfl.neural_network import cli, service
    directory=Path('/var/lib/vita-fl-fixtures/model-io')
    directory.mkdir(parents=True,exist_ok=True)
    os.environ['THESIS_NODE_SERVER_DIR']=str(directory)
    private=Path('/run/vita-fl/participant-private.pem')
    public=cli.public_der_from_private_key(private).hex()
    plain=cli.model_to_bytes(cli.random_model())
    input_path=Path('/run/vita-fl/evaluation-model.bin')
    input_path.touch(mode=0o600,exist_ok=True);input_path.write_bytes(plain)
    package_path=directory/'model-package.bin'
    with contextlib.redirect_stdout(io.StringIO()):
        cli.encrypt_model_package(input_path,package_path,'0x'+public,private_key=str(private))
    package=package_path.read_bytes()
    response=service._receive_model({'device_id':'0x'+'12'*20,'package_base64':base64.b64encode(package).decode(),
                                    'private_key':str(private),'expected_model_sha256':hashlib.sha256(plain).hexdigest()})
    saved=cli.received_models_dir()/('wb_client_0x'+'12'*20+'.bin')
    check("production_model_receive",response['ok'] and saved.read_bytes()==plain)
    # This is an observed limitation, not an expected security success.
    plaintext_persistent=saved.exists() and saved.stat().st_size==len(plain)
    return {"schema_version":1,"challenge":challenge,"checks":checks,
            "status":"PASS" if all(c['passed'] for c in checks) else "FAIL",
            "storage_observations":{"production_receiver_writes_plaintext":plaintext_persistent,
                "plaintext_on_fixture_persistent_volume":plaintext_persistent,"model_bytes":len(plain)},
            "session_evidence_base64":base64.b64encode(evidence).decode(),
            "boundary":"production cryptographic/session/model-IO components in dedicated Phala workload; not a full receiver HTTP/TLS integration or production-disk forensic test"}


if __name__ == '__main__':
    operation,challenge=sys.argv[1:]
    if len(challenge)!=64 or any(c not in '0123456789abcdef' for c in challenge):
        raise ValueError('32-byte challenge required')
    print(json.dumps(keys(challenge) if operation=='keys' else security(challenge),separators=(',',':')))
