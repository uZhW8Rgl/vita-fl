"""Exercise the actual receiver entrypoint without exposing a TCP listener."""

import http.client
import os
import socket
import stat
import threading
import time

import uvicorn
from fastapi import FastAPI

from tee_inference.service import __main__ as entrypoint


def test_receiver_serves_only_a_private_unix_socket(tmp_path, monkeypatch):
    directory = tmp_path / "receiver"
    directory.mkdir(mode=0o755)
    path = directory / "inference.sock"
    app = FastAPI()

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    server = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, log_level="error"))
    monkeypatch.setattr(entrypoint, "Path", lambda _: path)
    monkeypatch.setattr(entrypoint, "from_environment", lambda: app)
    monkeypatch.setattr(entrypoint.uvicorn, "Server", lambda _: server)
    failures = []

    def run():
        try:
            entrypoint.serve()
        except BaseException as exc:
            failures.append(exc)

    original_umask = os.umask(0o077)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and not failures and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not failures
        assert server.started
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        listeners = [sock for listener in server.servers for sock in listener.sockets]
        assert len(listeners) == 1
        assert listeners[0].family == socket.AF_UNIX
        connection = http.client.HTTPConnection("localhost", timeout=2)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(2)
        connection.sock.connect(str(path))
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == b'{"status":"ok"}'
        finally:
            connection.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        os.umask(original_umask)
    assert not thread.is_alive()
    assert not failures
    assert not path.exists()
