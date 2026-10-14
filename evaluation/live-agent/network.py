"""Test-image-only DNS faults for exactly the configured evidence dependencies.

Exposed through the evaluation-enabled agent behind the existing authenticated
UI. No arbitrary hostname, file, command, key access or signing operation.
"""

import hashlib
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException


def install(app):
    original = Path("/etc/hosts").read_text()
    state = {"mode": "none"}

    @app.post("/api/evaluation/network/{mode}")
    async def network(mode: str, payload: dict):
        from evaluation_context import enabled

        if not enabled():
            raise HTTPException(404)
        if (
            mode not in {"none", "appraisal_unavailable", "log_unavailable"}
            or set(payload) != {"challenge"}
            or not re.fullmatch("[a-f0-9]{64}", payload.get("challenge", ""))
        ):
            raise HTTPException(400)
        target = (
            "cloud-api.phala.com" if mode == "appraisal_unavailable" else urlsplit(os.environ["SCITT_URL"]).hostname
        )
        Path("/etc/hosts").write_text(original + ("\n127.0.0.1 " + target + "\n" if mode != "none" else ""))
        state["mode"] = mode
        addresses = sorted({x[4][0] for x in socket.getaddrinfo(target, 443)}) if mode != "none" else []
        return {
            "schema_version": 1,
            "case": mode,
            "challenge": payload["challenge"],
            "mode": mode,
            "target_hostname": target if mode != "none" else None,
            "resolved_only_loopback": addresses == ["127.0.0.1"] if mode != "none" else False,
            "hosts_sha256": hashlib.sha256(Path("/etc/hosts").read_bytes()).hexdigest(),
        }
