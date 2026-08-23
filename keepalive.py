#!/usr/bin/env python3
"""
supabase-keepalive - stop Supabase free-tier projects from being paused.

Supabase pauses a free project after 7 days without *database* activity.
This script generates that activity on a schedule. Stdlib only, no deps.

Two ways to point it at a project:

  1. TOKEN MODE (recommended - one token, every project you own):
       SUPABASE_ACCESS_TOKEN=sbp_...  python keepalive.py
     Lists every project via the Management API, runs `select now()` on each
     healthy one, and (with --restore) un-pauses ones already INACTIVE.
     Narrow it with --project <ref | dashboard URL | *.supabase.co URL>.

  2. TARGET MODE (no token - a project URL + its anon/service key):
       python keepalive.py --target https://abc.supabase.co,eyJ...,[table]
     Hits PostgREST. A table name makes it a real DB read; without one it
     only pings the API root, which is weaker evidence of "activity".

Exit code is non-zero if any project failed to ping, so cron/CI notices.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

MGMT = "https://api.supabase.com/v1"
REF_RE = re.compile(r"\b([a-z]{20})\b")
TIMEOUT = 30


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def http(method: str, url: str, headers: dict | None = None, body: dict | None = None):
    """Return (status, parsed-json-or-text). Never raises on HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "supabase-keepalive/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode(errors="replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        status = e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"network error: {e}"
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw


def parse_ref(value: str) -> str | None:
    """Accept a bare ref, a dashboard URL, or https://<ref>.supabase.co."""
    m = REF_RE.search(value.strip())
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# token mode
# --------------------------------------------------------------------------- #
@dataclass
class Project:
    ref: str
    name: str
    status: str


def list_projects(token: str) -> list[Project]:
    status, body = http("GET", f"{MGMT}/projects", {"Authorization": f"Bearer {token}"})
    if status != 200:
        sys.exit(f"ERROR: could not list projects (HTTP {status}): {body}\n"
                 "Check SUPABASE_ACCESS_TOKEN - make it at "
                 "https://supabase.com/dashboard/account/tokens")
    return [Project(p["id"], p.get("name", "?"), p.get("status", "?")) for p in body]


def ping_via_token(token: str, ref: str) -> tuple[bool, str]:
    status, body = http(
        "POST",
        f"{MGMT}/projects/{ref}/database/query",
        {"Authorization": f"Bearer {token}"},
        {"query": "select now() as pinged_at", "read_only": True},
    )
    if status in (200, 201):
        when = body[0].get("pinged_at") if isinstance(body, list) and body else "ok"
        return True, f"db says {when}"
    return False, f"HTTP {status}: {str(body)[:200]}"


def restore_via_token(token: str, ref: str) -> tuple[bool, str]:
    status, body = http("POST", f"{MGMT}/projects/{ref}/restore",
                        {"Authorization": f"Bearer {token}"})
    if status in (200, 201, 202):
        return True, "restore requested (takes a few minutes; ping again later)"
    return False, f"HTTP {status}: {str(body)[:200]}"


def run_token_mode(token: str, only: list[str], restore: bool, list_only: bool) -> int:
    projects = list_projects(token)
    if only:
        bad = [v for v in only if parse_ref(v) is None]
        if bad:
            sys.exit(f"ERROR: could not find a project ref in: {bad}")
        wanted = {parse_ref(v) for v in only}
        unknown = wanted - {p.ref for p in projects}
        if unknown:
            log(f"WARN: token cannot see these refs (wrong account/org?): {sorted(unknown)}")
        projects = [p for p in projects if p.ref in wanted]

    if not projects:
        log("No projects to ping.")
        return 1

    width = max(len(p.name) for p in projects)
    failures = 0
    for p in projects:
        tag = f"{p.name:<{width}}  {p.ref}  [{p.status}]"
        if list_only:
            log(tag)
            continue
        if p.status == "ACTIVE_HEALTHY":
            ok, msg = ping_via_token(token, p.ref)
        elif p.status == "INACTIVE" and restore:
            ok, msg = restore_via_token(token, p.ref)
        elif p.status == "INACTIVE":
            ok, msg = False, "paused - rerun with --restore to un-pause it"
        else:
            ok, msg = False, f"skipped (status {p.status}, not pingable right now)"
        failures += 0 if ok else 1
        log(f"{'OK  ' if ok else 'FAIL'}  {tag}  -> {msg}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# target mode
# --------------------------------------------------------------------------- #
def run_target_mode(targets: list[str]) -> int:
    failures = 0
    for spec in targets:
        parts = [s.strip() for s in spec.split(",")]
        if len(parts) < 2 or not parts[0].startswith("http"):
            log(f"FAIL  bad --target (want URL,KEY[,TABLE]): {spec[:60]}")
            failures += 1
            continue
        url, key = parts[0].rstrip("/"), parts[1]
        table = parts[2] if len(parts) > 2 else None
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        if table:
            status, body = http("GET", f"{url}/rest/v1/{table}?select=*&limit=1", headers)
            what = f"select from {table}"
        else:
            status, body = http("GET", f"{url}/rest/v1/", headers)
            what = "API root (no table given - add one for a real DB read)"
        ok = 200 <= status < 300
        failures += 0 if ok else 1
        log(f"{'OK  ' if ok else 'FAIL'}  {url}  -> {what}, HTTP {status}"
            + ("" if ok else f": {str(body)[:200]}"))
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", action="append", default=[],
                    help="ref / dashboard URL / *.supabase.co URL to limit token mode to (repeatable)")
    ap.add_argument("--target", action="append", default=[],
                    help="URL,KEY[,TABLE] for token-less mode (repeatable)")
    ap.add_argument("--restore", action="store_true", help="un-pause INACTIVE projects (token mode)")
    ap.add_argument("--list", action="store_true", help="just list projects + status, no ping")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    targets = a.target or [t for t in os.environ.get("SUPABASE_TARGETS", "").split(";") if t.strip()]

    if not token and not targets:
        sys.exit(
            "ERROR: nothing to do.\n"
            "  Either set SUPABASE_ACCESS_TOKEN (personal access token from\n"
            "  https://supabase.com/dashboard/account/tokens - pings ALL your projects)\n"
            "  or pass --target https://<ref>.supabase.co,<anon-key>[,<table>]\n"
            "  (or SUPABASE_TARGETS='url,key,table;url,key,table')."
        )

    rc = 0
    if token:
        rc |= run_token_mode(token, a.project, a.restore, a.list)
    if targets:
        rc |= run_target_mode(targets)
    return rc


def selftest() -> int:
    cases = {
        "abcdefghijklmnopqrst": "abcdefghijklmnopqrst",
        "https://supabase.com/dashboard/project/lzyvawfyrisuyikjsgtk/editor": "lzyvawfyrisuyikjsgtk",
        "https://ffrsffycrpevjhfyklxs.supabase.co": "ffrsffycrpevjhfyklxs",
        "https://ffrsffycrpevjhfyklxs.supabase.co/rest/v1/": "ffrsffycrpevjhfyklxs",
        "not a ref": None,
        "https://supabase.com/dashboard": None,
    }
    for inp, want in cases.items():
        got = parse_ref(inp)
        assert got == want, f"parse_ref({inp!r}) = {got!r}, want {want!r}"
    log(f"selftest: {len(cases)} parse_ref cases OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
