"""Private inference API: only the same-container TLS proxy can reach it."""

import os
import socket
from pathlib import Path

import uvicorn

from tee_inference.service.app import from_environment


def serve() -> None:
    os.umask(0o077)
    path = Path("/run/vita-fl/inference.sock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.unlink(missing_ok=True)
    # Uvicorn's uds option chmods its socket to 0666. Bind it ourselves so the
    # receiver keeps both its private directory and its socket restricted.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        server = uvicorn.Server(uvicorn.Config(from_environment(), proxy_headers=False))
        try:
            server.run(sockets=[listener])
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    serve()
