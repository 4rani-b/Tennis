# Tennis Court Checker — Claude Instructions

## Checking if the workflow is running

When the user asks "is it running?" or "is it checking?":

1. Get the current UTC time (check system time or note it from context).
2. Fetch the most recent workflow runs from GitHub Actions.
3. Compare the `created_at` timestamp of the latest run against the current time.
4. The self-chain fires every ~20 seconds, so if the latest run is **more than ~5 minutes old**, the chain has died.
5. If the chain is dead, trigger it immediately with `mcp__github__actions_run_trigger` (run_workflow, check_courts.yml, ref: main) without waiting to be asked.
6. Report: last run time, gap since then, and whether you triggered a restart.

Never report "it's running" based on stale data without verifying the timestamp is recent.
