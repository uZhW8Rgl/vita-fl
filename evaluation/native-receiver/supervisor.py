"""Dedicated test-image supervisor: restart only the fixed native receiver.

Keys, ML and TLS proxy stay in the same workload. No arbitrary process command,
path selection, shell execution or secret read endpoint exists.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

stopping = False
child = None
restart = threading.Event()
mode = "none"
fault_hits = 0
upstream = os.environ["KUBO_API"].rstrip("/")
hosts_original = Path("/etc/hosts").read_text()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/state":
            self.send_error(404)
            return
        data = json.dumps(
            {
                "pid": child.pid if child and child.poll() is None else None,
                "mode": mode,
                "fault_hits": fault_hits,
                "log_dns_blocked": mode == "log_unavailable"
                and sorted({x[4][0] for x in socket.getaddrinfo(urlsplit(os.environ["SCITT_URL"]).hostname, 443)})
                == ["127.0.0.1"],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global mode, fault_hits
        n = int(self.headers.get("Content-Length", "0"))
        if n > 1024:
            self.send_error(400)
            return
        body = json.loads(self.rfile.read(n))
        if self.path == "/restart" and body == {}:
            threading.Timer(0.5, restart.set).start()
        elif (
            self.path == "/mode"
            and body.get("mode")
            in {
                "none",
                "missing_receipt",
                "missing_artifact",
                "substituted_artifact",
                "wrong_key_round",
                "log_unavailable",
                "changed_inference_response",
            }
            and set(body) == {"mode"}
        ):
            mode = body["mode"]
            fault_hits = 0
            hostname = urlsplit(os.environ["SCITT_URL"]).hostname
            # A real DNS/connection failure for exactly the configured log endpoint.
            Path("/etc/hosts").write_text(
                hosts_original + ("\n127.0.0.1 " + hostname + "\n" if mode == "log_unavailable" else "")
            )
        elif (
            self.path == "/effect"
            and body == {"mode": mode}
            and mode in {"missing_receipt", "log_unavailable", "changed_inference_response"}
        ):
            fault_hits += 1
        else:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class StorageProxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        global fault_hits
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length) if length else b""
        request = urllib.request.Request(upstream + self.path, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(64 * 1024 * 1024)
                status = response.status
            if (
                mode in {"missing_artifact", "substituted_artifact", "wrong_key_round"}
                and urlsplit(self.path).path == "/api/v0/cat"
            ):
                from agent.blockchain_source import read_current_bundle_from_contract

                bundle = read_current_bundle_from_contract(
                    os.environ["RPC_URL"], os.environ["EXPECTED_GM_STORAGE_ADDRESS"]
                )
                arg = parse_qs(urlsplit(self.path).query).get("arg", [""])[0]
                selected = bundle["key_bundle_cid"] if mode == "wrong_key_round" else bundle["model_cid"]
                if arg.removeprefix("/ipfs/") == selected:
                    fault_hits += 1
                    if mode == "missing_artifact":
                        raw = b'{"Message":"chapter7 controlled missing artifact"}'
                        status = 404
                    elif mode == "substituted_artifact":
                        raw = bytes([raw[0] ^ 1]) + raw[1:]
                    else:
                        doc = json.loads(raw)
                        doc["round"] += 1
                        raw = json.dumps(doc, separators=(",", ":")).encode()
        except Exception:
            status = 502
            raw = b'{"Message":"test storage proxy upstream unavailable"}'
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def stop(*args):
    global stopping
    stopping = True
    if child and child.poll() is None:
        child.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
server = ThreadingHTTPServer(("127.0.0.1", 8002), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
proxy = ThreadingHTTPServer(("127.0.0.1", 8003), StorageProxy)
threading.Thread(target=proxy.serve_forever, daemon=True).start()
try:
    while not stopping:
        child = subprocess.Popen(
            [sys.executable, "/evaluation/native-receiver/receiver.py"],
            env={**os.environ, "KUBO_API": "http://127.0.0.1:8003"},
        )
        while not stopping and child.poll() is None and not restart.is_set():
            time.sleep(0.05)
        requested = restart.is_set()
        restart.clear()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if not stopping and not requested:
            raise RuntimeError("Native receiver exited without controlled restart")
finally:
    stop()
    server.shutdown()
    proxy.shutdown()
    Path("/etc/hosts").write_text(hosts_original)
