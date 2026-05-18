#!/usr/bin/env python3
"""Extract RTMR3 from a hex-encoded Intel TDX quote v4."""

from __future__ import annotations

import argparse
from pathlib import Path


HEADER_SIZE = 48
RTMR3_OFFSET_IN_BODY = 472
RTMR_SIZE = 48


def read_quote(path: Path) -> bytes:
    quote_hex = path.read_text(encoding="utf-8").strip()
    if quote_hex.startswith(("0x", "0X")):
        quote_hex = quote_hex[2:]
    quote_hex = "".join(quote_hex.split())
    return bytes.fromhex(quote_hex)


def extract_rtmr3(quote: bytes) -> bytes:
    offset = HEADER_SIZE + RTMR3_OFFSET_IN_BODY
    end = offset + RTMR_SIZE
    if len(quote) < end:
        raise ValueError(f"Quote is too short for RTMR3 extraction: {len(quote)} bytes")
    return quote[offset:end]


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract RTMR3 from a hex-encoded Intel TDX quote v4.")
    parser.add_argument("quote", type=Path, help="Path to a hex-encoded TDX quote")
    args = parser.parse_args()

    rtmr3 = extract_rtmr3(read_quote(args.quote))
    print(f"0x{rtmr3.hex()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
