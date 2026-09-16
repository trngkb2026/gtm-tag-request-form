#!/usr/bin/env python3
import base64
import importlib.util
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("weekly-lp-dispatch.py")
SPEC = importlib.util.spec_from_file_location("weekly_lp_dispatch", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
weekly = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(weekly)


class FakeClient:
    def __init__(self, *, schedule=False, active=None, dispatches=None):
        self.schedule = schedule
        self.active = active or {}
        self.dispatches = dispatches or []
        self.post_calls = 0

    def get_json(self, endpoint, params=()):
        if endpoint.endswith(f"workflows/{weekly.WORKFLOW_ID}"):
            return {"state": "active"}
        if endpoint.endswith(weekly.WORKFLOW_PATH):
            trigger = "  schedule:\n    - cron: '0 22 * * 0'\n" if self.schedule else "  workflow_dispatch: {}\n"
            if not self.schedule:
                trigger = "  workflow_dispatch: {}\n"
            body = f"on:\n{trigger}"
            return {"encoding": "base64", "content": base64.b64encode(body.encode()).decode()}
        if "/runs/" in endpoint:
            run_id = int(endpoint.rsplit("/", 1)[1])
            return {"id": run_id, "event": "workflow_dispatch", "head_branch": "main", "status": "completed", "conclusion": "success", "created_at": "2026-09-21T00:00:01Z"}
        if endpoint.endswith("/runs"):
            status = next((value.split("=", 1)[1] for value in params if value.startswith("status=")), None)
            if status:
                runs = self.active.get(status, [])
                return {"total_count": len(runs), "workflow_runs": runs}
            return {"total_count": len(self.dispatches), "workflow_runs": self.dispatches}
        raise AssertionError(endpoint)

    def post_dispatch(self):
        self.post_calls += 1


class RecordingDispatchClient(FakeClient):
    def __init__(self, state_dir):
        super().__init__()
        self.state_dir = state_dir

    def post_dispatch(self):
        pending = weekly.load_state(self.state_dir).get("pending")
        assert isinstance(pending, dict), "pending state must exist before POST"
        prepared_at = weekly.parse_github_timestamp(pending["prepared_at"])
        self.post_calls += 1
        self.dispatches.append(
            {
                "id": 123,
                "event": "workflow_dispatch",
                "head_branch": "main",
                "status": "queued",
                "conclusion": None,
                "created_at": (prepared_at + weekly.dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            }
        )


class WeeklyDispatchTests(unittest.TestCase):
    cutover = datetime.fromisoformat("2026-09-16T00:00:00+09:00")
    monday = datetime.fromisoformat("2026-09-21T07:00:00+09:00")

    def test_schedule_blocks_preflight_without_post(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(schedule=True)
            result, code = weekly.execute(client, Path(directory), self.cutover, self.monday, False)
        self.assertEqual((result["status"], code, client.post_calls), ("HOLD_SCHEDULE_PRESENT", 2, 0))

    def test_active_run_blocks_dispatch(self):
        queued = {"id": 22, "event": "workflow_dispatch", "head_branch": "main", "status": "queued", "conclusion": None, "created_at": "2026-09-21T00:00:00Z"}
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(active={"queued": [queued]})
            result, code = weekly.execute(client, Path(directory), self.cutover, self.monday, False)
        self.assertEqual((result["status"], code, client.post_calls), ("HOLD_ACTIVE_RUN", 2, 0))

    def test_dry_run_is_get_only(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            result, code = weekly.execute(
                client,
                Path(directory),
                self.cutover,
                datetime.fromisoformat("2026-09-16T10:00:00+09:00"),
                True,
            )
        self.assertEqual((result["status"], code, client.post_calls), ("DRY_RUN_READY", 0, 0))

    def test_pending_without_observed_run_never_reposts(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            weekly.write_state(state_dir, {"version": 1, "slots": {}, "pending": {"slot": weekly.slot_key(self.monday), "pre_dispatch_run_ids": [], "prepared_at": "2026-09-21T00:00:00Z"}})
            client = FakeClient()
            result, code = weekly.execute(client, state_dir, self.cutover, self.monday, False)
        self.assertEqual((result["status"], code, client.post_calls), ("HOLD_PENDING_NO_RUN", 2, 0))

    def test_pending_with_one_new_run_records_success(self):
        run = {"id": 99, "event": "workflow_dispatch", "head_branch": "main", "status": "completed", "conclusion": "success", "created_at": "2026-09-21T00:00:01Z"}
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            weekly.write_state(state_dir, {"version": 1, "slots": {}, "pending": {"slot": weekly.slot_key(self.monday), "pre_dispatch_run_ids": [], "prepared_at": "2026-09-21T00:00:00Z"}})
            client = FakeClient(dispatches=[run])
            result, code = weekly.execute(client, state_dir, self.cutover, self.monday, False)
            state = weekly.load_state(state_dir)
        self.assertEqual((result["status"], code, client.post_calls), ("TERMINAL_SUCCESS", 0, 0))
        self.assertIn(weekly.slot_key(self.monday), state["slots"])

    def test_post_has_durable_pending_state_and_only_one_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            client = RecordingDispatchClient(state_dir)
            result, code = weekly.execute(client, state_dir, self.cutover, self.monday, False)
            state = weekly.load_state(state_dir)
        self.assertEqual((result["status"], code, client.post_calls), ("AWAITING_TERMINAL", 0, 1))
        self.assertNotIn("pending", state)
        self.assertEqual(state["slots"][weekly.slot_key(self.monday)]["run"]["id"], 123)

    def test_multiple_post_dispatch_candidates_hold_without_retry(self):
        runs = [
            {"id": 31, "event": "workflow_dispatch", "head_branch": "main", "status": "queued", "conclusion": None, "created_at": "2026-09-21T00:00:01Z"},
            {"id": 32, "event": "workflow_dispatch", "head_branch": "main", "status": "queued", "conclusion": None, "created_at": "2026-09-21T00:00:02Z"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            weekly.write_state(state_dir, {"version": 1, "slots": {}, "pending": {"slot": weekly.slot_key(self.monday), "pre_dispatch_run_ids": [], "prepared_at": "2026-09-21T00:00:00Z"}})
            client = FakeClient(dispatches=runs)
            result, code = weekly.execute(client, state_dir, self.cutover, self.monday, False)
        self.assertEqual((result["status"], code, client.post_calls), ("HOLD_PENDING_AMBIGUOUS", 2, 0))

    def test_cutover_does_not_backfill_previous_week(self):
        before_first_slot = datetime.fromisoformat("2026-09-16T10:00:00+09:00")
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            result, code = weekly.execute(client, Path(directory), self.cutover, before_first_slot, False)
        self.assertEqual((result["status"], code, client.post_calls), ("NOT_YET_ELIGIBLE", 0, 0))

    def test_monday_before_seven_uses_the_previous_due_slot(self):
        monday_before_seven = datetime.fromisoformat("2026-09-28T06:59:00+09:00")
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            result, code = weekly.execute(
                client, Path(directory), self.cutover, monday_before_seven, True
            )
        self.assertEqual((result["status"], code), ("DRY_RUN_READY", 0))
        self.assertEqual(result["slot"], "2026-09-21T07:00:00+09:00")

    def test_late_or_wrong_branch_dispatch_cannot_resolve_pending(self):
        late_other_branch = {"id": 44, "event": "workflow_dispatch", "head_branch": "review/initial-code-review", "status": "completed", "conclusion": "success", "created_at": "2026-09-21T00:11:00Z"}
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            weekly.write_state(state_dir, {"version": 1, "slots": {}, "pending": {"slot": weekly.slot_key(self.monday), "pre_dispatch_run_ids": [], "prepared_at": "2026-09-21T00:00:00Z"}})
            client = FakeClient(dispatches=[late_other_branch])
            result, code = weekly.execute(client, state_dir, self.cutover, self.monday, False)
        self.assertEqual((result["status"], code, client.post_calls), ("HOLD_PENDING_NO_RUN", 2, 0))

    def test_incomplete_run_pagination_fails_closed(self):
        class IncompleteClient(FakeClient):
            def get_json(self, endpoint, params=()):
                if endpoint.endswith("/runs"):
                    page = next((value.split("=", 1)[1] for value in params if value.startswith("page=")), "1")
                    run = {"id": 1, "event": "workflow_dispatch", "head_branch": "main", "status": "completed", "conclusion": "success", "created_at": "2026-09-21T00:00:01Z"}
                    return {"total_count": 2, "workflow_runs": [run] if page == "1" else []}
                return super().get_json(endpoint, params)

        with self.assertRaises(weekly.GhError):
            weekly.workflow_runs(IncompleteClient())

    def test_gh_timeout_is_converted_to_safe_error(self):
        with mock.patch.object(weekly.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 60)):
            with self.assertRaises(weekly.GhError):
                weekly.GhClient("gh").get_json("repos/example/example")

    def test_post_timeout_keeps_pending_and_never_reposts(self):
        class TimeoutPostClient(FakeClient):
            def post_dispatch(self):
                self.post_calls += 1
                raise weekly.GhError("gh_api_timeout")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            client = TimeoutPostClient()
            result, code = weekly.execute(client, state_dir, self.cutover, self.monday, False)
            state = weekly.load_state(state_dir)
        self.assertEqual((result["status"], code, client.post_calls), ("HOLD_PENDING_NO_RUN", 2, 1))
        self.assertIn("pending", state)


if __name__ == "__main__":
    unittest.main()
