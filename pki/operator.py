"""Run on the operator's PKI host, never inside worker or agent images."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .runtime import atomic_write


def run(command, *, env=None):
    result = subprocess.run(command, env=env, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"Operator {Path(command[0]).name} command failed")
    return result.stdout


def initialize(args):
    offline, online = args.offline.resolve(), args.online.resolve()
    if offline == online or offline in online.parents or online in offline.parents:
        raise ValueError("Offline root and online intermediate need disjoint directories")
    if offline.exists() or online.exists():
        raise ValueError("Refusing to overwrite an existing CA directory")
    os.umask(0o077)
    offline.mkdir(parents=True, mode=0o700)
    online.mkdir(parents=True, mode=0o700)
    parsed = urlsplit(args.ca_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("CA URL must be an HTTPS origin")
    env = dict(os.environ, STEPPATH=str(offline))
    run(
        [
            "step",
            "ca",
            "init",
            "--deployment-type",
            "standalone",
            "--name",
            "VITA-FL Transport CA",
            "--dns",
            parsed.hostname,
            "--address",
            args.address,
            "--provisioner",
            "vita-fl-agent-operator",
            "--password-file",
            str(args.password_file.resolve()),
            "--provisioner-password-file",
            str(args.password_file.resolve()),
            "--with-ca-url",
            args.ca_url,
        ],
        env=env,
    )
    run(
        [
            "step",
            "ca",
            "provisioner",
            "add",
            "vita-fl-worker-operator",
            "--type",
            "JWK",
            "--create",
            "--ca-config",
            str(offline / "config/ca.json"),
            "--password-file",
            str(args.password_file.resolve()),
        ],
        env=env,
    )
    for directory in ("certs", "secrets", "config", "templates", "db"):
        (online / directory).mkdir(mode=0o700)
    for name in ("root_ca.crt", "intermediate_ca.crt"):
        shutil.copy2(offline / "certs" / name, online / "certs" / name)
    shutil.copy2(offline / "secrets/intermediate_ca_key", online / "secrets/intermediate_ca_key")
    # The online process never receives the root private key. Keep its encrypted
    # password on the operator host and mount only an intermediate password file.
    config = json.loads((offline / "config/ca.json").read_text())
    config.update(
        {
            "root": str(online / "certs/root_ca.crt"),
            "crt": str(online / "certs/intermediate_ca.crt"),
            "key": str(online / "secrets/intermediate_ca_key"),
            "crl": {"enabled": True, "generateOnRevoke": True, "cacheDuration": "2m", "renewPeriod": "30s"},
        }
    )
    config["db"]["dataSource"] = str(online / "db")
    config["authority"]["claims"] = {
        "minTLSCertDuration": "5m",
        "maxTLSCertDuration": "1h",
        "defaultTLSCertDuration": "1h",
        "disableRenewal": False,
        "allowRenewalAfterExpiry": False,
    }
    for provisioner in config["authority"]["provisioners"]:
        role = "agent" if provisioner["name"] == "vita-fl-agent-operator" else "inference"
        template = online / f"templates/{role}.tpl"
        template_text = (
            """{{- $expected := printf "urn:vita-fl:ROLE:%s" .Subject.CommonName -}}
{{- $found := false -}}
{{- range .SANs -}}
  {{- if eq .Type "uri" -}}
    {{- if ne .Value $expected -}}{{ fail "wrong VITA-FL certificate role" }}{{- end -}}
    {{- $found = true -}}
  {{- else -}}
    {{- if eq "ROLE" "agent" -}}{{ fail "agent certificates cannot request DNS identities" }}{{- end -}}
  {{- end -}}
{{- end -}}
{{- if not $found -}}{{ fail "VITA-FL URI identity is required" }}{{- end -}}
{
  "subject": {{ toJson .Subject }},
  "sans": {{ toJson .SANs }},
  "keyUsage": ["digitalSignature"],
  "extKeyUsage": EKUS,
  "basicConstraints": {"isCA": false},
  "crlDistributionPoints": [CRL_URL]
}
""".replace("CRL_URL", json.dumps(args.ca_url.rstrip("/") + "/1.0/crl"))
            .replace("ROLE", role)
            .replace(
                "EKUS",
                json.dumps(["clientAuth"] if role == "agent" else ["serverAuth", "clientAuth"]),
            )
        )
        atomic_write(template, template_text.encode(), 0o644)
        provisioner["options"] = {"x509": {"templateFile": str(template)}}
    atomic_write(online / "config/ca.json", json.dumps(config, indent=2).encode())
    # Provisioner token creation uses the offline copy. Its only keys are on the
    # operator machine; application images receive one scoped one-use token.
    print("Initialized offline root and separate online intermediate configuration.")
    print("Start step-ca with the online ca.json and its private password file.")


def issue_token(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.subject):
        raise ValueError("Invalid application subject")
    role = "agent" if args.role == "agent" else "inference"
    command = [
        "step",
        "ca",
        "token",
        args.subject,
        "--san",
        f"urn:vita-fl:{role}:{args.subject}",
        "--provisioner",
        f"vita-fl-{args.role}-operator",
        "--provisioner-password-file",
        str(args.password_file.resolve()),
        "--ca-url",
        args.ca_url,
        "--root",
        str(args.root.resolve()),
        "--not-after",
        "5m",
    ]
    if args.role == "worker":
        if not args.dns or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", args.dns):
            raise ValueError("A worker token must pin its exact TLS DNS name")
        command.extend(["--san", args.dns])
    elif args.dns:
        raise ValueError("Agent enrollment must not authorize server DNS names")
    if args.csr:
        command.extend(["--cnf-file", str(args.csr.resolve())])
    # Capture stdout; a bearer credential must never appear in terminal output.
    token = run(command).strip()
    if token.count(b".") != 2:
        raise ValueError("step returned an invalid enrollment token")
    atomic_write(args.output, token + b"\n")
    print("Scoped one-use enrollment token written to the requested private file.")


def revoke(args):
    if not re.fullmatch(r"[0-9]+", args.serial):
        raise ValueError("Provide the certificate's decimal serial number")
    token = (
        run(
            [
                "step",
                "ca",
                "token",
                args.serial,
                "--revoke",
                "--provisioner",
                f"vita-fl-{args.role}-operator",
                "--provisioner-password-file",
                str(args.password_file.resolve()),
                "--ca-url",
                args.ca_url,
                "--root",
                str(args.root.resolve()),
                "--not-after",
                "5m",
            ]
        )
        .strip()
        .decode("ascii")
    )
    from transport_security.client import _NoRedirect

    context = ssl.create_default_context(cafile=str(args.root))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )
    request = urllib.request.Request(
        args.ca_url.rstrip("/") + "/1.0/revoke",
        data=json.dumps(
            {
                "serial": args.serial,
                "ott": token,
                "reasonCode": 1,
                "reason": "Key compromise",
                "passive": True,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("CA rejected certificate revocation")
    print("Certificate revoked; the configured CA publishes an updated signed CRL.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    init = commands.add_parser("init")
    init.add_argument("--offline", type=Path, required=True)
    init.add_argument("--online", type=Path, required=True)
    init.add_argument("--password-file", type=Path, required=True)
    init.add_argument("--ca-url", required=True)
    init.add_argument("--address", default=":9000")
    token = commands.add_parser("token")
    token.add_argument("role", choices=("agent", "worker"))
    token.add_argument("subject")
    token.add_argument("--dns")
    token.add_argument("--csr", type=Path)
    token.add_argument("--ca-url", required=True)
    token.add_argument("--root", type=Path, required=True)
    token.add_argument("--password-file", type=Path, required=True)
    token.add_argument("--output", type=Path, required=True)
    revoke_command = commands.add_parser("revoke")
    revoke_command.add_argument("role", choices=("agent", "worker"))
    revoke_command.add_argument("serial")
    revoke_command.add_argument("--ca-url", required=True)
    revoke_command.add_argument("--root", type=Path, required=True)
    revoke_command.add_argument("--password-file", type=Path, required=True)
    args = parser.parse_args()
    {"init": initialize, "token": issue_token, "revoke": revoke}[args.operation](args)


if __name__ == "__main__":
    main()
