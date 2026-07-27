#!/usr/bin/env bash
set -euo pipefail

case "${TEE_INFERENCE_ENABLED:-0}" in
    1|true|TRUE|yes|YES)
        if [ -s "${PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH:-/run/vita-fl/participant-private.pem}" ]; then
            exec curl --fail --silent --show-error http://127.0.0.1:8080/healthz
        fi
        exec curl --fail --silent --show-error http://127.0.0.1:8000/health
        ;;
    *)
        exec curl --fail --silent --show-error http://127.0.0.1:8000/health
        ;;
esac
