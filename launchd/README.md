# Weekly LP update launchd candidate

This directory moves only the *trigger* of `Weekly LP List Update` from GitHub
Actions to a logged-in Mac launchd agent.  The workflow itself, its jobs,
permissions, secrets, and the Apps Script-dependent business flow remain in
GitHub Actions and GAS respectively.

## Release contract

`com.ten.gtm-weekly-lp-update.plist` deliberately points to one immutable
release directory, never to this checkout.  Before loading it, copy this
directory to the exact path shown in the plist:

`/Users/tr/.local/share/ten-gtm-tag-request-form/releases/20260916/launchd/`

The listed `--cutover-at` is the candidate cutover boundary.  The person
performing the approved production cutover must set it to the actual activation
time if that differs.  That timestamp prevents a first load from backfilling a
week whose Monday 07:00 JST slot occurred before cutover.  A later start in the
first eligible week performs at most one catch-up dispatch.

The plist invokes at Monday 07:00 JST and every five minutes.  The frequent
invocations only provide catch-up after sleep or login; the durable state and
non-blocking lock permit one dispatch per Monday slot.

## Dispatch safety

Every invocation first uses GitHub GET requests to confirm that production's
workflow is active, still has `workflow_dispatch`, and has no `schedule` key.
It also checks this workflow for `queued`, `in_progress`, `waiting`, `pending`,
and `requested` runs.  The run listing is paginated to its reported
`total_count`; a missing, changing, incomplete, or duplicated page is a HOLD.
Any active run stops the invocation.

Immediately before the POST, the wrapper atomically records a pending attempt
and the prior manual-dispatch run IDs in a private `0700` state directory.  A
GitHub CLI call has a 60-second timeout; a timed-out POST remains pending and
is never retried automatically.  After a POST it accepts exactly one new run
only when it is `workflow_dispatch` on `main` and was created from the pending
timestamp through the following ten minutes.  Zero or multiple candidates are
HOLD states and require investigation.  This prevents a later person's manual
run from being recorded as this dispatch.  Later GET requests update only
allow-listed run metadata and report whether the run completed successfully.

The wrapper invokes `gh` without accepting a token argument.  Authentication
stays inside `gh`; stdout contains only repository, workflow ID, timestamp,
status, slot, and optional run metadata.

## Local preflight and activation boundary

Before the production change, run a GET-only preflight from the immutable
release path:

```sh
/usr/bin/python3 /Users/tr/.local/share/ten-gtm-tag-request-form/releases/20260916/launchd/weekly-lp-dispatch.py \
  --cutover-at 2026-09-16T00:00:00+09:00 --dry-run
```

Before the workflow branch is merged, this correctly returns
`HOLD_SCHEDULE_PRESENT`.  After the merged workflow is read back from `main`,
the same command must return `DRY_RUN_READY` (or a documented safe hold such
as an existing active run).  Loading the plist, dispatching a real run, and
changing GitHub are separate approved production actions.
