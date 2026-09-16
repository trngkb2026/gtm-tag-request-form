#!/usr/bin/env python3
import base64
import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


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
            return {"id": run_id, "status": "completed", "conclusion": "success", "created_at": "2026-09-21T00:00:01Z"}
        if endpoint.endswith("/runs"):
            status = next((value.split("=", 1)[1] for value in params if value.startswith("status=")), None)
            if status:
                return {"workflow_runs": self.active.get(status, [])}
            return {"workflow_runs": self.dispatches}
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
        self.post_calls += 1
        self.dispatches.append(
            {
                "id": 123,
                "event": "workflow_dispatch",
                "status": "queued",
                "conclusion": None,
                "created_at": "2026-09-21T00:00:01Z",
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
        queued = {"id": 22, "status": "queued", "conclusion": None, "created_at": "2026-09-21T00:00:00Z"}
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
        self.assertEqual((result["status"], code, client.post_calls), ("HOLD_PENDING_NO_RUN", 0, 0))

    def test_pending_with_one_new_run_records_success(self):
        run = {"id": 99, "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "created_at": "2026-09-21T00:00:01Z"}
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
            {"id": 31, "event": "workflow_dispatch", "status": "queued", "conclusion": None, "created_at": "2026-09-21T00:00:01Z"},
            {"id": 32, "event": "workflow_dispatch", "status": "queued", "conclusion": None, "created_at": "2026-09-21T00:00:02Z"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            weekly.write_state(state_dir, {"version": 1, "slots": {}, "pending": {"slot": weekly.slot_key(self.monday), "pre_dispatch_run_ids": [], "prepared_at": "2026-09-21T00:00:00Z"}})
            client = FakeClient(dispatches=runs)
            result, code = weekly.execute(client, state_dir, self.cutover, self.monday, False)
        self.assertEqual((result["status"], code, client.post_calls), ("HOLD_PENDING_AMBIGUOUS", 0, 0))

    def test_cutover_does_not_backfill_previous_week(self):
        before_first_slot = datetime.fromisoformat("2026-09-16T10:00:00+09:00")
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            result, code = weekly.execute(client, Path(directory), self.cutover, before_first_slot, False)
        self.assertEqual((result["status"], code, client.post_calls), ("NOT_YET_ELIGIBLE", 0, 0))


if __name__ == "__main__":
    unittest.main()
