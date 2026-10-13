"""DuckDNS routing for the runtime's optional, TEE-terminated HTTPS ingress.

The DNS token stays in the deployment runner, never in the measured workload.
The gateway forwards TLS to the instance identified by the routing TXT record.
"""

from __future__ import annotations

import ipaddress
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

INGRESS_IMAGE = (
    "ghcr.io/dstack-tee/dstack-ingress@sha256:c212abb7bedec4d7b54a82bcf9972e58e39a4757cc811faeb6017eee6c0673b0"
)
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.duckdns\.org")
GATEWAY = re.compile(r"gateway\.[a-z0-9-]+\.phala\.network")
TOKEN = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
IDENTITY = re.compile(r"[0-9a-f]{40}")


def validate_settings(domain: str, gateway: str, token: str) -> None:
    if not DOMAIN.fullmatch(domain):
        raise ValueError("PHALA_UI_DOMAIN must be a single DuckDNS subdomain.")
    if not GATEWAY.fullmatch(gateway):
        raise ValueError("PHALA_UI_GATEWAY_DOMAIN must name a Phala gateway.")
    if not TOKEN.fullmatch(token):
        raise ValueError("DUCKDNS_TOKEN must be a valid account token.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_dns(name: str, rr_type: int) -> list[str]:
    query = urllib.parse.urlencode({"name": name, "type": rr_type})
    try:
        with urllib.request.urlopen("https://dns.google/resolve?" + query, timeout=20) as response:
            data = json.load(response)
        if data.get("Status") not in (0, 3):
            raise ValueError
        return [answer["data"] for answer in data.get("Answer", []) if answer.get("type") == rr_type]
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("Could not resolve the public UI routing records.") from None


class DuckDNS:
    def __init__(self, domain: str, token: str):
        self.name = domain.removesuffix(".duckdns.org")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def update(self, **values: str) -> None:
        query = urllib.parse.urlencode({"domains": self.name, "token": self.token, **values})
        request = urllib.request.Request("https://www.duckdns.org/update?" + query)
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=30) as response:
                    result = response.read(1024).decode("ascii").strip()
                if result != "OK":
                    raise ValueError("DuckDNS rejected the UI DNS update.")
                return
            except urllib.error.HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504}:
                    raise ValueError(f"DuckDNS update failed (HTTP {error.code}).") from None
            except OSError:
                pass
            if attempt < 3:
                time.sleep(5 * 2**attempt)
        # Never format exceptions or request URLs: they contain the token.
        raise ValueError("DuckDNS update failed after retries.")


def routing_target(metadata: dict[str, Any], expected_app_id: str, gateway: str) -> str:
    instance = metadata.get("instance_id")
    info = metadata.get("gateway") or {}
    if (
        metadata.get("name") != "master-thesis-contract-runtime-phala"
        or str(metadata.get("app_id", "")).removeprefix("app_") != expected_app_id
        or not isinstance(instance, str)
        or not IDENTITY.fullmatch(instance)
        or info.get("cname") != gateway
    ):
        raise ValueError("UI domain target does not match the runtime identity and gateway.")
    return instance + ":443"


def configure_dns(client, *, cvm_id: str, app_id: str, domain: str, gateway: str, token: str) -> str:
    validate_settings(domain, gateway, token)
    metadata = client.request("GET", "/cvms/" + urllib.parse.quote(cvm_id, safe=""))
    target = routing_target(metadata, app_id, gateway)
    ipv4 = [value for value in public_dns(gateway, 1) if ipaddress.IPv4Address(value).is_global]
    ipv6 = [value for value in public_dns(gateway, 28) if ipaddress.IPv6Address(value).is_global]
    if not ipv4:
        raise ValueError("Phala gateway has no public IPv4 address for DuckDNS.")
    dns = DuckDNS(domain, token)
    # An old AAAA record must not send IPv6 clients to the previous service.
    if not ipv6 and public_dns(domain, 28):
        dns.update(clear="true")
    addresses = {"ip": sorted(ipv4)[0]}
    if ipv6:
        addresses["ipv6"] = sorted(ipv6)[0]
    dns.update(**addresses)
    dns.update(txt=target)
    print(f"DuckDNS configured: {domain} -> {gateway}, instance {target}", flush=True)
    return "https://" + domain


def wait_for_https(domain: str, timeout: float = 1200) -> None:
    """Require a publicly trusted certificate and the UI's authentication gate."""
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    while True:
        try:
            with opener.open("https://" + domain + "/", timeout=15):
                raise ValueError("Custom UI origin did not require authentication.")
        except urllib.error.HTTPError as error:
            if error.code == 401 and error.headers.get("WWW-Authenticate") == 'Basic realm="Master Thesis DFL"':
                print(f"Stable UI: https://{domain} (trusted HTTPS; authentication required)", flush=True)
                return
        except (OSError, ssl.SSLError):
            pass
        if time.monotonic() >= deadline:
            raise ValueError("Custom UI HTTPS is not ready; inspect ingress and retry the deployment.")
        time.sleep(15)
