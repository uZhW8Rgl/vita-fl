#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 2 ]]; then
  echo "Usage: $0 [KUBO_API_URL] [OUTPUT_DIR]" >&2
  exit 2
fi

KUBO_API_URL="${1:-${PHALA_RUNTIME_KUBO_API_URL:-${KUBO_API:-}}}"
OUTPUT_DIR="${2:-phala}"

if [[ -z "${KUBO_API_URL}" ]]; then
  echo "KUBO_API_URL is required. Pass it as the first argument or set PHALA_RUNTIME_KUBO_API_URL/KUBO_API." >&2
  exit 2
fi

KUBO_API_URL="${KUBO_API_URL%/}"
mkdir -p "${OUTPUT_DIR}"

fetch_mfs_file() {
  local mfs_path="$1"
  local output_path="$2"
  local encoded_path
  encoded_path="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${mfs_path}")"
  curl -fsS -X POST "${KUBO_API_URL}/api/v0/files/read?arg=${encoded_path}" -o "${output_path}"
}

fetch_mfs_file "/phala-artifacts/latest/app_code.txt" "${OUTPUT_DIR}/app_code.txt"
fetch_mfs_file "/phala-artifacts/latest/rtmr3_event_log.txt" "${OUTPUT_DIR}/rtmr3_event_log.txt"

echo "Updated ${OUTPUT_DIR}/app_code.txt and ${OUTPUT_DIR}/rtmr3_event_log.txt from ${KUBO_API_URL}."
