"""Build a public Pages redirect from the latest successful Phala deployment."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PUBLISHERS = frozenset(
    {
        "Publish Phala Agent",
        "Publish Phala Control API",
        "Publish DFL Worker Image",
        "Publish Smart Contracts Image",
        "Publish TEE Inference Image",
        "Publish Phala Transparency Log",
        "Publish Phala UI",
        "Publish ZK Inference Image",
    }
)
MAX_RESPONSE = 16 * 1024 * 1024
UI_LINE = re.compile(r"^(?:\S+\s+)?UI: (https://\S+)\s*$", re.MULTILINE)
UI_HOST = re.compile(r"[0-9a-f]{40}-8088\.[a-z0-9-]+\.phala\.network")


def validate_endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not UI_HOST.fullmatch(parsed.netloc)
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected an HTTPS Phala UI gateway origin on port 8088.")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the GitHub token to the signed log-storage URL.
        return None


class GitHub:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected an owner/repository name.")
        if not token:
            raise ValueError("GH_TOKEN is required to read Actions deployment logs.")
        self.repository = repository
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def read(self, path: str) -> bytes:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code != 302:
                raise ValueError(f"GitHub Actions read failed (HTTP {error.code}).") from None
            location = error.headers.get("Location", "")
            parsed = urllib.parse.urlsplit(location)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("GitHub returned an invalid log download URL.") from None
            # This second request contains no Authorization header.
            response = self.opener.open(urllib.request.Request(location), timeout=30)
        with response:
            content = response.read(MAX_RESPONSE + 1)
        if len(content) > MAX_RESPONSE:
            raise ValueError("GitHub Actions response exceeds the supported size.")
        return content

    def document(self, path: str) -> dict:
        result = json.loads(self.read(path))
        if not isinstance(result, dict):
            raise ValueError("GitHub returned an invalid Actions response.")
        return result


def latest_endpoint(github: GitHub, branch: str) -> tuple[str, int]:
    query = urllib.parse.urlencode({"branch": branch, "per_page": 100})
    runs = github.document(f"actions/runs?{query}").get("workflow_runs", [])
    candidates = []
    for run in runs:
        if run.get("name") not in PUBLISHERS or run.get("head_branch") != branch or run.get("conclusion") != "success":
            continue
        # API order is creation order; an older build can finish after a newer
        # one. Choose by deployment-job completion, not by commit/run number.
        jobs = github.document(f"actions/runs/{int(run['id'])}/jobs?per_page=100").get("jobs", [])
        candidates.extend(
            job
            for job in jobs
            if job.get("name") == "Deploy published image to Phala / deploy" and job.get("conclusion") == "success"
        )
    for job in sorted(candidates, key=lambda item: item.get("completed_at") or "", reverse=True):
        logs = github.read(f"actions/jobs/{int(job['id'])}/logs").decode("utf-8")
        endpoints = UI_LINE.findall(logs)
        if endpoints:
            return validate_endpoint(endpoints[-1]), int(job["run_id"])
        # A successful stale-build skip prints no UI URL. It must not replace
        # the most recent deployment that actually reconciled the runtime.
    raise ValueError("No successful Phala deployment with a UI endpoint was found in the latest 100 runs.")


def check_ui(endpoint: str) -> None:
    request = urllib.request.Request(endpoint + "/ui-health")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        if response.status != 200 or response.read(32).strip() != b"ok":
            raise ValueError("The deployed UI did not pass its public health check.")


def render_site(directory: Path, endpoint: str) -> None:
    endpoint = validate_endpoint(endpoint)
    directory.mkdir(parents=True, exist_ok=True)
    destination = html.escape(endpoint + "/", quote=True)
    page = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<noscript><meta http-equiv="refresh" content="0;url={destination}"></noscript>
<title>VITA-FL</title>
<body>
<p>Opening VITA-FL. <a href="{destination}" rel="noreferrer">Open the UI</a>.</p>
<script>
// Read the current target without reusing a cached endpoint document.
fetch('./endpoint.json?t=' + Date.now(), {{cache: 'no-store'}})
  .then(response => {{if (!response.ok) throw new Error(); return response.json();}})
  .then(data => location.replace(data.url + '/' + location.hash))
  .catch(() => location.replace({json.dumps(endpoint + "/")} + location.hash));
</script>
</body>
</html>
"""
    (directory / "index.html").write_text(page, encoding="utf-8")
    (directory / "endpoint.json").write_text(json.dumps({"url": endpoint}) + "\n", encoding="utf-8")
    (directory / ".nojekyll").touch()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--branch", default=os.environ.get("PHALA_DEPLOY_BRANCH", "phala_app_key"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        github = GitHub(args.repository, os.environ.get("GH_TOKEN", ""))
        endpoint, run_id = latest_endpoint(github, args.branch)
        check_ui(endpoint)
        render_site(args.output, endpoint)
        print(f"Pages target: {endpoint} (deployment run {run_id})")
    except (OSError, ValueError, urllib.error.URLError):
        # Neither deployment logs, credentials nor signed download URLs belong
        # in the public Pages artifact or error output.
        print(
            "Could not prepare the UI redirect. Check successful Phala deployments and UI readiness.", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
