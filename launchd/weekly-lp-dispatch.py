#!/usr/bin/env python3
"""Safely dispatch the weekly LP update GitHub Actions workflow from launchd.

The program intentionally stores only allow-listed GitHub run metadata.  It
never reads, exports, or logs the credentials that ``gh`` obtains internally.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence


JST = dt.timezone(dt.timedelta(hours=9), name="JST")
REPOSITORY = "trngkb2026/gtm-tag-request-form"
WORKFLOW_ID = 254684659
WORKFLOW_PATH = ".github/workflows/weekly-lp-update.yml"
DEFAULT_STATE_DIR = Path(
    "/Users/tr/Library/Application Support/TEN-WORKS/gtm-tag-request-form-launchd"
)
ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
STATUS_VALUES = frozenset(
    {
        "NOT_YET_ELIGIBLE",
        "HOLD_ACTIVE_RUN",
        "HOLD_GH_GET_FAILED",
        "HOLD_POST_UNKNOWN",
        "HOLD_PENDING_AMBIGUOUS",
        "HOLD_PENDING_NO_RUN",
        "HOLD_SCHEDULE_PRESENT",
        "HOLD_WORKFLOW_INACTIVE",
        "HOLD_WORKFLOW_DISPATCH_MISSING",
        "DISPATCHED_AWAITING_RUN",
        "AWAITING_TERMINAL",
        "TERMINAL_SUCCESS",
        "TERMINAL_FAILURE",
        "DRY_RUN_READY",
    }
)


class GhError(RuntimeError):
    """A GitHub CLI call failed without exposing its output."""


class GhClient:
    def __init__(self, gh_bin: str) -> None:
        self.gh_bin = gh_bin

    def _run(self, args: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                [self.gh_bin, "api", *args],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as error:
            raise GhError("gh_api_timeout") from error
        if completed.returncode != 0:
            raise GhError("gh_api_failed")
        return completed.stdout

    def get_json(self, endpoint: str, params: Sequence[str] = ()) -> dict[str, Any]:
        output = self._run(["--method", "GET", endpoint, *params])
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise GhError("gh_api_invalid_json") from error
        if not isinstance(value, dict):
            raise GhError("gh_api_unexpected_json")
        return value

    def post_dispatch(self) -> None:
        self._run(
            [
                "--method",
                "POST",
                f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW_ID}/dispatches",
                "-f",
                "ref=main",
            ]
        )


def parse_jst_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(JST)


def weekly_slot(now: dt.datetime) -> dt.datetime:
    local_now = now.astimezone(JST)
    monday = (local_now - dt.timedelta(days=local_now.weekday())).date()
    candidate = dt.datetime.combine(monday, dt.time(7, 0), JST)
    return candidate if candidate <= local_now else candidate - dt.timedelta(days=7)


def first_slot_at_or_after(cutover_at: dt.datetime) -> dt.datetime:
    candidate = weekly_slot(cutover_at)
    if candidate < cutover_at:
        candidate += dt.timedelta(days=7)
    return candidate


def slot_key(slot: dt.datetime) -> str:
    return slot.astimezone(JST).isoformat(timespec="seconds")


def utc_now_iso(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_run_metadata(run: dict[str, Any]) -> dict[str, Any]:
    run_id = run.get("id")
    if not isinstance(run_id, int):
        raise GhError("workflow_run_missing_id")
    status = run.get("status")
    conclusion = run.get("conclusion")
    created_at = run.get("created_at")
    event = run.get("event")
    head_branch = run.get("head_branch")
    if (
        not isinstance(status, str)
        or not isinstance(created_at, str)
        or not isinstance(event, str)
        or not isinstance(head_branch, str)
    ):
        raise GhError("workflow_run_missing_metadata")
    try:
        parse_github_timestamp(created_at)
    except ValueError as error:
        raise GhError("workflow_run_invalid_timestamp") from error
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion if isinstance(conclusion, str) else None,
        "created_at": created_at,
        "event": event,
        "head_branch": head_branch,
    }


def parse_github_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(dt.timezone.utc)


def state_path(state_dir: Path) -> Path:
    return state_dir / "weekly-lp-dispatch-state.json"


def ensure_private_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)


def load_state(state_dir: Path) -> dict[str, Any]:
    path = state_path(state_dir)
    if not path.exists():
        return {"version": 1, "slots": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GhError("state_unreadable") from error
    if not isinstance(value, dict) or not isinstance(value.get("slots"), dict):
        raise GhError("state_invalid")
    return value


def write_state(state_dir: Path, state: dict[str, Any]) -> None:
    ensure_private_state_dir(state_dir)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".weekly-lp-state-", dir=state_dir)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, state_path(state_dir))
    finally:
        temporary_path.unlink(missing_ok=True)


@contextlib.contextmanager
def exclusive_lock(state_dir: Path) -> Iterator[bool]:
    ensure_private_state_dir(state_dir)
    lock_path = state_dir / "weekly-lp-dispatch.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def decode_workflow(content_response: dict[str, Any]) -> str:
    content = content_response.get("content")
    encoding = content_response.get("encoding")
    if not isinstance(content, str) or encoding != "base64":
        raise GhError("workflow_content_unavailable")
    try:
        return base64.b64decode(content, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise GhError("workflow_content_invalid") from error


def verify_production_workflow(client: GhClient) -> None:
    workflow = client.get_json(f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW_ID}")
    if workflow.get("state") != "active":
        raise GhError("workflow_inactive")
    content = decode_workflow(client.get_json(f"repos/{REPOSITORY}/contents/{WORKFLOW_PATH}"))
    if re.search(r"(?m)^\s*schedule\s*:", content):
        raise GhError("workflow_schedule_present")
    if not re.search(r"(?m)^\s*workflow_dispatch\s*:", content):
        raise GhError("workflow_dispatch_missing")


def workflow_runs(client: GhClient, status: str | None = None) -> list[dict[str, Any]]:
    endpoint = f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW_ID}/runs"
    collected: list[dict[str, Any]] = []
    expected_total: int | None = None
    page = 1
    while expected_total is None or len(collected) < expected_total:
        params: list[str] = ["-f", "per_page=100", "-f", f"page={page}"]
        if status is not None:
            params.extend(["-f", f"status={status}"])
        response = client.get_json(endpoint, params)
        total_count = response.get("total_count")
        runs = response.get("workflow_runs")
        if not isinstance(total_count, int) or total_count < 0 or not isinstance(runs, list):
            raise GhError("workflow_runs_unavailable")
        if expected_total is None:
            expected_total = total_count
        elif total_count != expected_total:
            raise GhError("workflow_runs_changed_during_pagination")
        if len(collected) + len(runs) > expected_total:
            raise GhError("workflow_runs_count_invalid")
        if expected_total > len(collected) and not runs:
            raise GhError("workflow_runs_pagination_incomplete")
        for run in runs:
            if not isinstance(run, dict):
                raise GhError("workflow_runs_invalid_item")
            collected.append(safe_run_metadata(run))
        page += 1
    if expected_total is None or len(collected) != expected_total:
        raise GhError("workflow_runs_pagination_incomplete")
    ids = [run["id"] for run in collected]
    if len(ids) != len(set(ids)):
        raise GhError("workflow_runs_duplicate_id")
    return collected


def active_runs(client: GhClient) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for status in sorted(ACTIVE_STATUSES):
        for run in workflow_runs(client, status):
            found[run["id"]] = run
    return list(found.values())


def dispatch_runs(client: GhClient) -> list[dict[str, Any]]:
    return [
        run
        for run in workflow_runs(client)
        if run.get("event") == "workflow_dispatch"
    ]


def run_by_id(client: GhClient, run_id: int) -> dict[str, Any]:
    return safe_run_metadata(
        client.get_json(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    )


def status_for_run(run: dict[str, Any]) -> str:
    if run["status"] != "completed":
        return "AWAITING_TERMINAL"
    return "TERMINAL_SUCCESS" if run["conclusion"] == "success" else "TERMINAL_FAILURE"


def public_result(status: str, now: dt.datetime, **extra: Any) -> dict[str, Any]:
    if status not in STATUS_VALUES:
        raise ValueError("invalid public status")
    result: dict[str, Any] = {
        "at": utc_now_iso(now),
        "repository": REPOSITORY,
        "workflow_id": WORKFLOW_ID,
        "status": status,
    }
    for key in ("active_run_count", "run_id", "run_status", "run_conclusion", "slot"):
        if key in extra:
            result[key] = extra[key]
    return result


def exit_code_for_result(result: dict[str, Any]) -> int:
    status = result["status"]
    return 2 if status.startswith("HOLD_") or status == "TERMINAL_FAILURE" else 0


def reconcile_pending(
    client: GhClient, state: dict[str, Any], slot: str, now: dt.datetime, state_dir: Path
) -> dict[str, Any]:
    pending = state.get("pending")
    if not isinstance(pending, dict) or pending.get("slot") != slot:
        raise GhError("pending_invalid")
    before_ids = pending.get("pre_dispatch_run_ids")
    prepared_at = pending.get("prepared_at")
    if (
        not isinstance(before_ids, list)
        or not all(isinstance(run_id, int) for run_id in before_ids)
        or not isinstance(prepared_at, str)
    ):
        raise GhError("pending_invalid")
    try:
        prepared_time = parse_github_timestamp(prepared_at)
    except ValueError as error:
        raise GhError("pending_invalid") from error
    latest_created_at = prepared_time + dt.timedelta(minutes=10)
    prior_ids = set(before_ids)
    candidates = [
        run
        for run in dispatch_runs(client)
        if run["id"] not in prior_ids
        and run["event"] == "workflow_dispatch"
        and run["head_branch"] == "main"
        and prepared_time <= parse_github_timestamp(run["created_at"]) <= latest_created_at
    ]
    if len(candidates) == 0:
        return public_result("HOLD_PENDING_NO_RUN", now, slot=slot)
    if len(candidates) > 1:
        return public_result("HOLD_PENDING_AMBIGUOUS", now, slot=slot)
    run = candidates[0]
    state["slots"][slot] = {"run": run, "recorded_at": utc_now_iso(now)}
    state.pop("pending", None)
    write_state(state_dir, state)
    return public_result(
        status_for_run(run),
        now,
        slot=slot,
        run_id=run["id"],
        run_status=run["status"],
        run_conclusion=run["conclusion"],
    )


def execute(
    client: GhClient,
    state_dir: Path,
    cutover_at: dt.datetime,
    now: dt.datetime,
    dry_run: bool,
) -> tuple[dict[str, Any], int]:
    slot_at = weekly_slot(now)
    first_slot = first_slot_at_or_after(cutover_at)
    slot = slot_key(slot_at)
    try:
        verify_production_workflow(client)
    except GhError as error:
        mapping = {
            "workflow_inactive": "HOLD_WORKFLOW_INACTIVE",
            "workflow_schedule_present": "HOLD_SCHEDULE_PRESENT",
            "workflow_dispatch_missing": "HOLD_WORKFLOW_DISPATCH_MISSING",
        }
        return public_result(mapping.get(str(error), "HOLD_GH_GET_FAILED"), now, slot=slot), 2

    if dry_run:
        try:
            active = active_runs(client)
        except GhError:
            return public_result("HOLD_GH_GET_FAILED", now, slot=slot), 2
        if active:
            return public_result("HOLD_ACTIVE_RUN", now, slot=slot, active_run_count=len(active)), 2
        return public_result("DRY_RUN_READY", now, slot=slot), 0
    if now.astimezone(JST) < first_slot:
        return public_result("NOT_YET_ELIGIBLE", now, slot=slot), 0

    try:
        state = load_state(state_dir)
    except GhError:
        return public_result("HOLD_GH_GET_FAILED", now, slot=slot), 2

    if isinstance(state.get("pending"), dict):
        if state["pending"].get("slot") != slot:
            return public_result("HOLD_POST_UNKNOWN", now, slot=slot), 2
        try:
            result = reconcile_pending(client, state, slot, now, state_dir)
            return result, exit_code_for_result(result)
        except GhError:
            return public_result("HOLD_GH_GET_FAILED", now, slot=slot), 2

    existing = state["slots"].get(slot)
    if isinstance(existing, dict) and isinstance(existing.get("run"), dict):
        run_value = existing["run"]
        run_id = run_value.get("id")
        if not isinstance(run_id, int):
            return public_result("HOLD_GH_GET_FAILED", now, slot=slot), 2
        try:
            run = run_by_id(client, run_id)
        except GhError:
            return public_result("HOLD_GH_GET_FAILED", now, slot=slot), 2
        existing["run"] = run
        existing["checked_at"] = utc_now_iso(now)
        write_state(state_dir, state)
        return (
            public_result(
                status_for_run(run),
                now,
                slot=slot,
                run_id=run["id"],
                run_status=run["status"],
                run_conclusion=run["conclusion"],
            ),
            0,
        )

    try:
        active = active_runs(client)
        prior_dispatches = dispatch_runs(client)
    except GhError:
        return public_result("HOLD_GH_GET_FAILED", now, slot=slot), 2
    if active:
        return public_result("HOLD_ACTIVE_RUN", now, slot=slot, active_run_count=len(active)), 2
    state["pending"] = {
        "slot": slot,
        "attempt_id": str(uuid.uuid4()),
        "prepared_at": utc_now_iso(now),
        "pre_dispatch_run_ids": [run["id"] for run in prior_dispatches],
    }
    write_state(state_dir, state)
    try:
        client.post_dispatch()
    except GhError:
        try:
            return reconcile_pending(client, state, slot, now, state_dir), 2
        except GhError:
            return public_result("HOLD_POST_UNKNOWN", now, slot=slot), 2
    try:
        result = reconcile_pending(client, state, slot, now, state_dir)
        return result, exit_code_for_result(result)
    except GhError:
        return public_result("HOLD_GH_GET_FAILED", now, slot=slot), 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutover-at", required=True, help="ISO-8601 timestamp with UTC offset")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--gh-bin", default="/opt/homebrew/bin/gh")
    parser.add_argument("--dry-run", action="store_true", help="GET-only production preflight")
    args = parser.parse_args(argv)
    try:
        cutover_at = parse_jst_timestamp(args.cutover_at)
    except ValueError:
        parser.error("--cutover-at must be an ISO-8601 timestamp with an offset")
    now = dt.datetime.now(JST)
    if args.dry_run:
        result, exit_code = execute(
            GhClient(args.gh_bin), args.state_dir, cutover_at, now, dry_run=True
        )
        print(json.dumps(result, sort_keys=True))
        return exit_code
    with exclusive_lock(args.state_dir) as acquired:
        if not acquired:
            print(json.dumps(public_result("HOLD_ACTIVE_RUN", now, active_run_count=1), sort_keys=True))
            return 2
        result, exit_code = execute(GhClient(args.gh_bin), args.state_dir, cutover_at, now, args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
