#!/usr/bin/env python3
"""Replay Phala/dstack RTMR3 event logs and compare them with a TDX quote."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from extract_tdx_rtmr3 import extract_rtmr3, read_quote


RTMR_SIZE = 48


def extract_event_objects(text: str) -> list[dict[str, Any]]:
    """Extract JSON event objects from copied Phala Trust Center text."""
    events: list[dict[str, Any]] = []
    for match in re.finditer(r"\{[^{}]*\}", text, flags=re.DOTALL):
        try:
            event = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if event.get("imr") == 3 and "digest" in event:
            events.append(event)
    return events


def extract_app_compose_object(text: str) -> dict[str, Any]:
    start = text.index("{")
    end_marker = "\n\nis_registered"
    end = text.index(end_marker) if end_marker in text else text.rindex("}") + 1
    return json.loads(text[start:end])


def replay_rtmr(events: list[dict[str, Any]]) -> bytes:
    rtmr = bytes(RTMR_SIZE)
    for event in events:
        digest = bytes.fromhex(str(event["digest"]))
        if len(digest) != RTMR_SIZE:
            raise ValueError(f"Invalid event digest length for {event.get('event')}: {len(digest)} bytes")
        rtmr = hashlib.sha384(rtmr + digest).digest()
    return rtmr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_log", type=Path, help="Copied Phala RTMR3 event-log text or JSON")
    parser.add_argument("--quote", type=Path, default=Path("data/phala_tdx_quote"), help="Hex-encoded TDX quote")
    parser.add_argument(
        "--compose",
        type=Path,
        default=Path("phala/dstack-compose.template.yml"),
        help="Local compose policy file used for documentation comparison",
    )
    parser.add_argument(
        "--app-code",
        type=Path,
        default=None,
        help="Copied Phala app-code text containing the measured compose_file object",
    )
    args = parser.parse_args()

    events = extract_event_objects(args.event_log.read_text(encoding="utf-8"))
    if not events:
        raise ValueError(f"No RTMR3 events found in {args.event_log}")

    replayed_rtmr3 = replay_rtmr(events)
    quote_rtmr3 = extract_rtmr3(read_quote(args.quote))
    compose_event = next((event for event in events if event.get("event") == "compose-hash"), None)
    local_compose_hash = hashlib.sha256(args.compose.read_bytes()).hexdigest() if args.compose.exists() else ""
    app_code_hash = ""
    app_code_compose_file_hash = ""
    app_code_image_pinned = False
    if args.app_code:
        app_code = extract_app_compose_object(args.app_code.read_text(encoding="utf-8"))
        app_code_bytes = json.dumps(app_code, sort_keys=True, separators=(",", ":")).encode("utf-8")
        app_code_hash = hashlib.sha256(app_code_bytes).hexdigest()
        compose_file = str(app_code.get("docker_compose_file", ""))
        if compose_file:
            app_code_compose_file_hash = hashlib.sha256(compose_file.encode("utf-8")).hexdigest()
        app_code_image_pinned = "@sha256:" in str(app_code.get("docker_compose_file", ""))

    print(f"events: {len(events)}")
    if compose_event:
        print(f"event compose-hash: 0x{compose_event['event_payload']}")
    if app_code_hash:
        print(f"app-code object sha256: 0x{app_code_hash}")
        print(f"app-code object matches event: {app_code_hash == compose_event.get('event_payload') if compose_event else False}")
        if app_code_compose_file_hash:
            print(f"app-code docker_compose_file sha256: 0x{app_code_compose_file_hash}")
            print(
                "app-code docker_compose_file matches local compose: "
                f"{app_code_compose_file_hash == local_compose_hash if local_compose_hash else False}"
            )
        print(f"app-code pins image digest: {app_code_image_pinned}")
    if local_compose_hash:
        print(f"local compose sha256: 0x{local_compose_hash}")
        print(f"local compose matches event: {local_compose_hash == compose_event.get('event_payload') if compose_event else False}")
    print(f"replayed rtmr3: 0x{replayed_rtmr3.hex()}")
    print(f"quote rtmr3:    0x{quote_rtmr3.hex()}")
    print(f"rtmr3 matches quote: {replayed_rtmr3 == quote_rtmr3}")

    checks = [replayed_rtmr3 == quote_rtmr3]
    if app_code_hash and compose_event:
        checks.append(app_code_hash == compose_event.get("event_payload"))
        checks.append(app_code_image_pinned)

    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
