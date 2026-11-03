from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def check_json(url: str, expected: dict[str, str]) -> None:
    with urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        payload = json.loads(response.read())
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"unexpected readiness response: {payload}")


def check_frontend(url: str) -> None:
    with urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        body = response.read(64_000).decode("utf-8", errors="replace")
    if "<html" not in body.lower() and "<!doctype html" not in body.lower():
        raise RuntimeError("response is not an HTML page")


def check_demo_login(frontend_url: str) -> None:
    env_file = Path(".env")
    if not env_file.is_file():
        raise RuntimeError(".env is missing; run scripts/bootstrap.py first")
    password = next(
        (
            line.partition("=")[2].strip().strip('"').strip("'")
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if line.startswith("DEMO_PASSWORD=")
        ),
        "",
    )
    if not password:
        raise RuntimeError("DEMO_PASSWORD is empty; seed the demo accounts first")
    operator_token = ""
    for email, role in (
        ("operator@demo.local", "operator"),
        ("reviewer@demo.local", "reviewer"),
    ):
        body = json.dumps(
            {"email": email, "password": password, "org_slug": "demo-a"}
        ).encode("utf-8")
        request = Request(
            f"{frontend_url.rstrip('/')}/api/v1/auth/login",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            raise RuntimeError(f"demo login returned HTTP {exc.code}") from None
        if payload.get("role") != role or payload.get("org_slug") != "demo-a":
            raise RuntimeError("demo login returned an unexpected role or organization")
        if role == "operator":
            operator_token = payload.get("access_token", "")
    if not operator_token:
        raise RuntimeError("operator login did not return an access token")

    sources_request = Request(
        f"{frontend_url.rstrip('/')}/api/v1/policies/sources",
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    with urlopen(sources_request, timeout=8) as response:
        sources = json.loads(response.read())
    if sources.get("synthetic") is not True or len(sources.get("sources", [])) != 7:
        raise RuntimeError(
            "policy source listing did not return the seven synthetic demo sources"
        )

    query_request = Request(
        f"{frontend_url.rstrip('/')}/api/v1/policies/query",
        data=json.dumps({"question": "so luong tren hoa don khac PO"}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {operator_token}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(query_request, timeout=8) as response:
        answer = json.loads(response.read())
    citations = answer.get("citations", [])
    if (
        answer.get("status") != "answered"
        or answer.get("generation") != "extractive"
        or not citations
        or citations[0].get("source_slug") != "discrepancy-handling"
        or citations[0].get("synthetic") is not True
    ):
        raise RuntimeError(
            "policy query did not return an extractive synthetic source citation"
        )

    copilot_request = Request(
        f"{frontend_url.rstrip('/')}/api/v1/copilot/status",
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    with urlopen(copilot_request, timeout=8) as response:
        copilot = json.loads(response.read())
    if (
        not isinstance(copilot.get("enabled"), bool)
        or not isinstance(copilot.get("max_rounds"), int)
        or not isinstance(copilot.get("max_tool_calls"), int)
        or copilot.get("requires_live_provider") is not True
    ):
        raise RuntimeError(
            "copilot status did not return its bounded live-provider configuration"
        )


def check_api_in_compose(docker_context: str | None) -> None:
    code = """
import json, urllib.request
for path, expected in (("/health/live", {"status": "alive"}), ("/health/ready", {"status": "ready", "database": "ok"})):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=5) as response:
        assert response.status == 200
        payload = json.loads(response.read())
    assert all(payload.get(key) == value for key, value in expected.items()), payload
"""
    command = ["docker"]
    if docker_context:
        command.extend(["--context", docker_context])
    command.extend(["compose", "exec", "-T", "api", "python", "-c", code])
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=20, check=False
    )
    if result.returncode:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"docker exited {result.returncode}"
        )
        raise RuntimeError(detail)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check local IDP API and frontend health endpoints."
    )
    parser.add_argument(
        "--api-url",
        help="Check an API URL directly instead of the API container on the active Compose context.",
    )
    parser.add_argument("--docker-context", default=os.getenv("DOCKER_CONTEXT"))
    parser.add_argument("--frontend-url", default="http://localhost:5173")
    parser.add_argument(
        "--demo-login",
        action="store_true",
        help="Verify both seeded demo roles through the frontend proxy using .env (never print credentials/tokens).",
    )
    args = parser.parse_args()
    if args.api_url:
        api_checks = [
            (
                "API liveness",
                lambda: check_json(
                    f"{args.api_url.rstrip('/')}/health/live", {"status": "alive"}
                ),
            ),
            (
                "API readiness",
                lambda: check_json(
                    f"{args.api_url.rstrip('/')}/health/ready",
                    {"status": "ready", "database": "ok"},
                ),
            ),
        ]
    else:
        api_checks = [
            (
                "API liveness/readiness",
                lambda: check_api_in_compose(args.docker_context),
            )
        ]
    checks = [*api_checks, ("Frontend", lambda: check_frontend(args.frontend_url))]
    if args.demo_login:
        checks.append(
            (
                "Seeded login, policy lookup, and copilot status",
                lambda: check_demo_login(args.frontend_url),
            )
        )
    failed = False
    for label, check in checks:
        try:
            check()
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            failed = True
            print(f"FAIL {label}: {exc}")
        else:
            print(f"PASS {label}")
    if failed:
        print(
            "Smoke checks failed. See docs/runbook.md for startup and health commands.",
            file=sys.stderr,
        )
        return 1
    print("Smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
