"""Native production receiver plus closed test-only restart/header controls."""

import hashlib
import hmac
import json
import os
import re
import socket
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Request
from tee_inference.service.app import from_environment
from tee_inference.service.attestation import DstackClient


def control(path, payload=None):
    r = urllib.request.Request(
        "http://127.0.0.1:8002" + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r, timeout=5) as response:
        return json.load(response)


app = from_environment()


@app.post("/v1/evaluation/receiver")
async def receiver_control(request: Request, payload: dict):
    expected = ("Bearer " + os.environ["PRIVATE_KEY"]).encode()
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
        raise HTTPException(401)
    if set(payload) != {"operation", "challenge"} or not re.fullmatch("[a-f0-9]{64}", payload.get("challenge", "")):
        raise HTTPException(400)
    operation = payload["operation"]
    if operation == "restart":
        control("/restart", {})
    elif operation in {
        "none",
        "missing_receipt",
        "missing_artifact",
        "substituted_artifact",
        "wrong_key_round",
        "log_unavailable",
    }:
        control("/mode", {"mode": operation})
    elif operation != "state":
        raise HTTPException(400)
    claims = {"schema_version": 1, "challenge": payload["challenge"], "operation": operation, **control("/state")}
    raw = json.dumps(claims, separators=(",", ":"))
    report = hashlib.sha512(b"VITA-FL:native-receiver-evaluation:v1\0" + raw.encode()).digest()
    client = DstackClient()
    quote = client.call("/GetQuote", {"report_data": report.hex()})
    info = client.call("/Info", {})
    tcb = info["tcb_info"]
    tcb = json.loads(tcb) if isinstance(tcb, str) else tcb
    return {
        "claims": claims,
        "claims_json": raw,
        "quote": quote["quote"],
        "event_log": quote["event_log"],
        "app_compose": tcb["app_compose"],
    }


class ReceiptFault:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        async def outgoing(message):
            if (
                message["type"] == "http.response.start"
                and scope.get("path") == "/v1/jobs"
                and control("/state")["mode"] == "missing_receipt"
            ):
                # Alter the live HTTP response after actual native operation and publication.
                message = {
                    **message,
                    "headers": [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower()
                        not in {b"x-sello-receipt", b"x-sello-scitt-bundle-url", b"x-sello-scitt-transaction"}
                    ],
                }
            await send(message)

        await self.inner(scope, receive, outgoing)


os.umask(0o077)
path = Path("/run/vita-fl/inference.sock")
path.unlink(missing_ok=True)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
    listener.bind(str(path))
    path.chmod(0o600)
    try:
        uvicorn.Server(uvicorn.Config(ReceiptFault(app), proxy_headers=False)).run(sockets=[listener])
    finally:
        path.unlink(missing_ok=True)
