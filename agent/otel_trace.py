"""Tiny OTLP trace exporter used by the local agent.

The project already sends raw OTLP JSON from the Node service. Keeping the
Python side equally small avoids another runtime dependency in the agent image.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

SPAN_KIND_CLIENT = 3
SPAN_KIND_SERVER = 2
_WARNED = False


def _endpoint() -> str:
    return os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")


def _trace_id() -> str:
    seed = f"agent:{os.getpid()}:{time.time_ns()}:{random.random()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _span_id() -> str:
    return f"{random.getrandbits(64):016x}"


def _attribute_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value if value is not None else "")}


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": key, "value": _attribute_value(value)} for key, value in values.items() if value is not None]


def _export_span(span: dict[str, Any], service_name: str) -> None:
    global _WARNED
    endpoint = _endpoint()
    if not endpoint:
        return

    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _attributes(
                        {
                            "service.name": service_name,
                            "deployment.environment": os.environ.get("DOCKER", "local"),
                        }
                    )
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "dfl-agent", "version": "1.0.0"},
                        "spans": [span],
                    }
                ],
            }
        ]
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        if not _WARNED:
            _WARNED = True
            print(f"OTel trace export failed: {exc}", file=sys.stderr)


def record_service_edge(
    name: str,
    *,
    source: str,
    target: str,
    start_time_ns: int,
    end_time_ns: int,
    status: str = "OK",
    attributes: dict[str, Any] | None = None,
) -> None:
    trace_id = _trace_id()
    client_span_id = _span_id()
    server_span_id = _span_id()
    base_attributes = {
        "dfl.operation": name,
        "dfl.duration_ms": max(0, (end_time_ns - start_time_ns) // 1_000_000),
        **(attributes or {}),
    }

    _export_span(
        {
            "traceId": trace_id,
            "spanId": client_span_id,
            "name": f"service.{name}",
            "kind": SPAN_KIND_CLIENT,
            "startTimeUnixNano": str(start_time_ns),
            "endTimeUnixNano": str(max(end_time_ns, start_time_ns)),
            "attributes": _attributes({**base_attributes, "peer.service": target}),
            "status": {"code": 2 if status == "ERROR" else 1},
        },
        source,
    )
    _export_span(
        {
            "traceId": trace_id,
            "spanId": server_span_id,
            "parentSpanId": client_span_id,
            "name": f"service.{name}",
            "kind": SPAN_KIND_SERVER,
            "startTimeUnixNano": str(start_time_ns),
            "endTimeUnixNano": str(max(end_time_ns, start_time_ns)),
            "attributes": _attributes(base_attributes),
            "status": {"code": 2 if status == "ERROR" else 1},
        },
        target,
    )


@contextlib.contextmanager
def service_edge(name: str, *, source: str, target: str, **attributes: Any) -> Iterator[None]:
    start = time.time_ns()
    status = "OK"
    try:
        yield
    except Exception:
        status = "ERROR"
        raise
    finally:
        record_service_edge(
            name,
            source=source,
            target=target,
            start_time_ns=start,
            end_time_ns=time.time_ns(),
            status=status,
            attributes=attributes,
        )
