#!/usr/bin/env python3
"""
Supabase keepalive for token-based GitHub Actions workflows.

Requires Python 3.10+ and the standard library only.

Configuration:
    SUPABASE_ACCESS_TOKEN: Supabase personal access token.

Examples:
    python keepalive.py --restore --project <project-ref>
    python keepalive.py --project <ref-1> --project <ref-2>
    python keepalive.py --list
    python keepalive.py --selftest

Behaviour:
    - Discover projects visible to the token.
    - Fail if any explicitly requested project is missing.
    - Query healthy projects with SELECT now().
    - Optionally request restoration once, wait, and verify a SQL query.
    - Retry transient errors for discovery and read-only SQL requests.
    - Continue checking other projects when one fails.
    - Exit 0 only when all selected projects pass.

This is best-effort automation, not a guarantee against inactivity pausing.
Target mode (--target / SUPABASE_TARGETS) is intentionally not supported.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


MGMT = "https://api.supabase.com/v1"

HTTP_TIMEOUT = 20
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 30
RESTORE_WAIT_SECONDS = 240
POLL_SECONDS = 15

TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
REF_RE = re.compile(r"[a-z]{20}")


class KeepaliveError(Exception):
    """An expected operational failure safe to display in logs."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward credential-bearing requests through redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirects())


@dataclass(frozen=True)
class Project:
    ref: str
    name: str
    status: str


def log(message: str) -> None:
    print(message, flush=True)


def safe_name(value: str) -> str:
    """Keep project names on one log line and prevent workflow commands."""
    value = "".join(c if c.isprintable() else " " for c in value)
    return value.replace("::", ": :")[:100]


def parse_ref(value: str) -> str:
    """Accept a bare ref or a recognised Supabase project/dashboard URL."""
    value = value.strip()

    if REF_RE.fullmatch(value):
        return value

    parsed = urllib.parse.urlsplit(value)

    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Invalid project reference or Supabase URL.")

    host = (parsed.hostname or "").lower()

    if host == "supabase.com":
        match = re.match(
            r"^/dashboard/project/([a-z]{20})(?:/|$)",
            parsed.path,
        )
        if match:
            return match.group(1)

    match = re.fullmatch(r"([a-z]{20})\.supabase\.co", host)
    if match:
        return match.group(1)

    raise ValueError("Invalid project reference or Supabase URL.")


def retry_delay(headers, attempt: int) -> float:
    """Respect Retry-After where practical, capped to a bounded delay."""
    fallback = float(2 ** attempt)
    value = headers.get("Retry-After") if headers else None

    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                seconds = (
                    when - datetime.now(timezone.utc)
                ).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = fallback

        return max(0.0, min(seconds, MAX_RETRY_DELAY))

    return min(fallback, MAX_RETRY_DELAY)


def remaining_time(deadline: float | None) -> float:
    if deadline is None:
        return float(HTTP_TIMEOUT)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise KeepaliveError("Operation deadline reached.")

    return remaining


def request_json(
    token: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    retry: bool = True,
    deadline: float | None = None,
):
    """
    Send a request to the fixed Management API host.

    Do not print response bodies: they may contain sensitive information.
    Restore POSTs use retry=False to avoid blindly repeating mutations.
    """
    attempts = MAX_ATTEMPTS if retry else 1
    encoded = json.dumps(body).encode("utf-8") if body is not None else None

    for attempt in range(1, attempts + 1):
        timeout = min(HTTP_TIMEOUT, remaining_time(deadline))

        req = urllib.request.Request(
            MGMT + path,
            data=encoded,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "supabase-keepalive/2.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

        response_headers = None

        try:
            with OPENER.open(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")

            if not raw.strip():
                return None

            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise KeepaliveError(
                    "API returned a successful response with invalid JSON."
                ) from None

        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = exc.headers
            exc.close()

            message = f"Management API returned HTTP {status}."

            if status not in TRANSIENT_STATUSES:
                raise KeepaliveError(message) from None

        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            OSError,
        ):
            message = "Network request failed or timed out."

        except (UnicodeError, ValueError):
            raise KeepaliveError(
                "Invalid API response or request configuration."
            ) from None

        if attempt == attempts:
            raise KeepaliveError(message)

        delay = retry_delay(response_headers, attempt)

        if deadline is not None:
            delay = min(delay, max(0.0, deadline - time.monotonic()))

        log(
            f"WARN  {message} Retrying "
            f"({attempt + 1}/{attempts}) after {delay:.0f}s."
        )
        time.sleep(delay)

    raise KeepaliveError("Request failed.")


def list_projects(
    token: str,
    *,
    deadline: float | None = None,
) -> list[Project]:
    body = request_json(token, "GET", "/projects", deadline=deadline)

    if not isinstance(body, list):
        raise KeepaliveError("Project-list response was not a JSON list.")

    projects = []
    seen = set()

    for item in body:
        if not isinstance(item, dict):
            raise KeepaliveError("Malformed project-list entry.")

        ref = item.get("id")
        name = item.get("name")
        status = item.get("status")

        if (
            not isinstance(ref, str)
            or not REF_RE.fullmatch(ref)
            or not isinstance(name, str)
            or not isinstance(status, str)
            or ref in seen
        ):
            raise KeepaliveError("Invalid or duplicate project-list entry.")

        seen.add(ref)
        projects.append(Project(ref, safe_name(name), status))

    return projects


def ping_project(
    token: str,
    ref: str,
    *,
    deadline: float | None = None,
) -> str:
    body = request_json(
        token,
        "POST",
        f"/projects/{ref}/database/query",
        {"query": "select now() as pinged_at", "read_only": True},
        deadline=deadline,
    )

    if (
        not isinstance(body, list)
        or len(body) != 1
        or not isinstance(body[0], dict)
        or not isinstance(body[0].get("pinged_at"), str)
        or not body[0]["pinged_at"].strip()
    ):
        raise KeepaliveError(
            "SQL response did not contain the expected timestamp."
        )

    return body[0]["pinged_at"]


def wait_and_ping(token: str, ref: str, deadline: float) -> str:
    """Wait for health, then verify SQL, within the recovery deadline."""
    last_status = None

    while time.monotonic() < deadline:
        try:
            projects = list_projects(token, deadline=deadline)
            project = next((p for p in projects if p.ref == ref), None)

            if project is None:
                raise KeepaliveError(
                    "Project disappeared from token visibility."
                )

            if project.status != last_status:
                log(f"INFO  {ref}: recovery status {project.status}")
                last_status = project.status

            if project.status == "ACTIVE_HEALTHY":
                return ping_project(token, ref, deadline=deadline)

        except KeepaliveError as exc:
            log(f"WARN  {ref}: recovery check: {exc}")

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_SECONDS, remaining))

    raise KeepaliveError(
        "Recovery deadline reached; database query was not verified. "
        "Restoration may still be in progress."
    )


def check_project(token: str, project: Project, restore: bool) -> str:
    if project.status == "ACTIVE_HEALTHY":
        return ping_project(token, project.ref)

    if not restore:
        raise KeepaliveError(
            f"Project is {project.status}, not ACTIVE_HEALTHY. "
            "Use --restore to recover paused projects."
        )

    deadline = time.monotonic() + RESTORE_WAIT_SECONDS

    if project.status == "INACTIVE":
        log(f"INFO  {project.name} {project.ref}: requesting restoration.")

        try:
            request_json(
                token,
                "POST",
                f"/projects/{project.ref}/restore",
                retry=False,
                deadline=deadline,
            )
            log(
                f"INFO  {project.ref}: restore request accepted; "
                "waiting for a verified database query."
            )
        except KeepaliveError as exc:
            # A timeout can occur after the server accepted the request.
            # Do not blindly send another restore request.
            log(
                f"WARN  {project.ref}: restore request not confirmed: {exc} "
                "Checking status without resubmitting."
            )
    else:
        log(
            f"INFO  {project.ref}: currently {project.status}; "
            "waiting for health without sending a restore request."
        )

    return wait_and_ping(token, project.ref, deadline)


def print_summary(
    expected: int,
    found: int,
    queried: int,
    missing: int,
    failed: int,
    list_only: bool,
) -> None:
    log("")
    log("SUMMARY")
    log(f"  Expected projects: {expected}")
    log(f"  Found projects: {found}")
    log(f"  Missing projects: {missing}")

    if list_only:
        log("  Mode: list only; no SQL queries or restores performed")
    else:
        log(f"  Successfully queried: {queried}")
        log(f"  Failed or unverified: {failed}")

    success = expected > 0 and missing == 0 and failed == 0
    log(f"  Result: {'PASS' if success else 'FAIL'}")


def selftest() -> int:
    ref = "abcdefghijklmnopqrst"

    valid = [
        ref,
        f"https://supabase.com/dashboard/project/{ref}",
        f"https://supabase.com/dashboard/project/{ref}/editor",
        f"https://{ref}.supabase.co",
        f"https://{ref}.supabase.co/rest/v1/",
    ]

    invalid = [
        "",
        "not a ref",
        f"prefix {ref} suffix",
        f"http://{ref}.supabase.co",
        f"https://example.com/dashboard/project/{ref}",
        f"https://{ref}.supabase.co.evil.example",
        f"https://user:password@{ref}.supabase.co",
    ]

    for value in valid:
        if parse_ref(value) != ref:
            raise KeepaliveError("Valid-reference self-test failed.")

    for value in invalid:
        try:
            parse_ref(value)
        except ValueError:
            continue
        raise KeepaliveError("Invalid-reference self-test failed.")

    log(
        f"Self-test passed: {len(valid) + len(invalid)} "
        "reference-parser cases. No network requests performed."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="REF_OR_URL",
        help="Expected project ref or Supabase URL; repeat for each project.",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Recover paused projects and wait for a verified SQL query.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List projects and statuses; do not query SQL or restore.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Test reference parsing without credentials or network access.",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    if not token:
        log("ERROR  SUPABASE_ACCESS_TOKEN is missing or empty.")
        return 1

    if os.environ.get("SUPABASE_TARGETS", "").strip():
        log(
            "ERROR  SUPABASE_TARGETS is configured, but this version "
            "supports token mode only. Remove that environment variable."
        )
        return 1

    try:
        requested = list(dict.fromkeys(parse_ref(v) for v in args.project))
    except ValueError:
        log("ERROR  Invalid --project value; use a ref or Supabase URL.")
        return 1

    try:
        projects = list_projects(token)
    except KeepaliveError as exc:
        log(f"ERROR  Could not discover projects: {exc}")
        log("RESULT FAIL: project coverage and SQL checks were not verified.")
        return 1

    visible = {p.ref: p for p in projects}

    if requested:
        missing_refs = [ref for ref in requested if ref not in visible]
        selected = [visible[ref] for ref in requested if ref in visible]
        expected = len(requested)
    else:
        log(
            "WARN  No --project arguments: checking all visible projects. "
            "Missing intended projects cannot be detected in this mode."
        )
        missing_refs = []
        selected = projects
        expected = len(selected)

    for ref in missing_refs:
        log(f"FAIL  {ref}: expected project is not visible to this token.")

    queried = 0
    failed = len(missing_refs)

    for project in selected:
        tag = f"{project.name} {project.ref} [{project.status}]"

        if args.list:
            log(f"LIST  {tag}")
            continue

        try:
            timestamp = pinged_at = check_project(
                token, project, args.restore
            )
            queried += 1
            log(
                f"OK    {project.name} {project.ref} "
                f"[ACTIVE_HEALTHY] -> db says {timestamp}"
            )
        except KeepaliveError as exc:
            failed += 1
            log(f"FAIL  {tag} -> {exc}")
        except Exception as exc:
            failed += 1
            # Only log the exception type, not possibly sensitive details.
            log(f"FAIL  {tag} -> unexpected {type(exc).__name__}")

    print_summary(
        expected=expected,
        found=len(selected),
        queried=queried,
        missing=len(missing_refs),
        failed=failed,
        list_only=args.list,
    )

    return 0 if expected > 0 and failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("ERROR  Interrupted.")
        sys.exit(130)
    except Exception as exc:
        log(f"ERROR  Unexpected {type(exc).__name__}; run not verified.")
        sys.exit(1)
