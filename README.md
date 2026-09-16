# SyllabAI Ops

Automation workspace for the [SyllabAI](https://github.com/SyllabAI) project.

| Workflow | What it does |
|---|---|
| `repo-mirror.yml` | Mirrors every SyllabAI repo's default branch to Google Drive — light lane every 30 min (repos ≤ 500 MB), heavy lane Saturdays 02:23 UTC (all repos, incl. multi-GB ones) + on-demand dispatch |
| `google-sheet-dashboard.yml` | Refreshes the project & sync dashboard (a private Google Sheet) daily at 08:43 Asia/Dhaka |
| `discord-digest.yml` | Posts the daily project pulse to Discord (~09:07 Asia/Dhaka): commits in 24h + Drive-sync health |
| `ci-quota-sentinel.yml` | Hourly canary that detects when the private repos' Actions quota is restored (billing anchor day 27; probe window opens 2026-09-27) and fires the pilot CI reruns + a Discord ping |
| `pilot-monitor.yml` | Pilot production monitoring — 19 read-only checks against the deployed Render backend + Vercel web, reported to Discord; 6-hourly full probe set + weekly ops run with the operator checklist |
| `keepalive.yml` | Monthly no-op commit so GitHub never auto-disables the schedules (60-day inactivity rule) |

**Why is this repo public?** GitHub Actions minutes are free and unlimited for
public repositories. Keeping scheduled automation here leaves the private
repos' included minutes for real CI.

## Pilot Monitor cutover (2026-09-16, session 77)

`pilot-monitor.yml` migrated here from the **private `syllabai-web` repo**
(whose workflow was retired in the same session) — the largest remaining
private-billed consumer at ~124 scheduled runs/month × ~4 min ≈ **~496
private minutes/month** (42% of the post-dashboard steady state). The probe
script is a byte-identical copy of the web repo's
`scripts/ops/pilot_probe.py` (sha256 `93379eaf…db8157`) — every check is
preserved: backend health (cold-start tolerant), CORS, auth-401, teacher
route guard, deployed-bundle markers, monitor-learner loop, subject scoping,
missing-param, cross-subject contamination, teacher concept-graph,
class-analytics, marking lane, weakness targeting, tutor LLM chain, weekly
teacher-activation idempotency, and the weekly operator checklist. Schedules
are identical (`17 */6 * * *` + Sun `33 9 * * 0`), as is the fail-loud
behavior: exit 1 after the Discord report whenever any check fails.

**One operator step remains** (secrets are write-only — nobody can copy them
via the API): add to *Settings → Secrets and variables → Actions*:

1. `PILOT_MONITOR_EMAIL` — `pilot.monitor@syllabai-test.dev` (the TEST-class
   monitor account; values are in the `syllabai-web` repo's secret list)
2. `PILOT_MONITOR_PASSWORD` — its credentials
3. `PILOT_TEACHER_EMAIL` / `PILOT_TEACHER_PASSWORD` — optional; unlock the
   teacher KG + activation checks

Until they are set, every scheduled run fails closed at the secrets
preflight with an actionable message — monitoring is visibly DOWN, never
silently green. The moment the secrets are added, the next 6-hourly run
reports to Discord automatically; nothing else to do. **Restore path** if
public-repo credential placement is ever rejected: revert the web-repo
commit that retired `.github/workflows/pilot-monitor.yml`.

If the probe ever needs to change, this repo's copy is canonical; the web
repo retains its historical copy under `scripts/ops/` for provenance.

## Central Drive mirroring

`repo-mirror.yml` replaces the per-repo `google-drive-sync` workflows that used
to bill a 60-min-timeout job on **every push to every branch** of the private
repos. One scheduled job clones each repo's default branch (depth 1) and
`rclone sync`s it to the same destinations the per-repo workflows used
(`gdrive:SyllabAI-GitHub/<repo>`, or a pinned folder / shared drive via
variables) — existing Drive folders keep working without migration.

- **Skip list**: variable `MIRROR_SKIP` (default `syllabai-ops,syllabai-resources`)
  — ops doesn't mirror itself, and `syllabai-resources` keeps its own
  push-triggered sync (it's a public repo, so that mirror is free and fresher).
- **Self-sync aware**: repos that still run their own active `Google Drive Sync`
  workflow are auto-skipped (no double-syncing the same Drive destination).
  When a repo's own workflow is retired (as `syllabai-core`'s was), the central
  mirror picks it up automatically on the next run — no config change needed.
- **Heavy repos** (> 500 MB, e.g. `syllabai-pastpapers` ≈ 4 GB) are deferred to
  the weekly heavy lane so the 30-min lane stays cheap. Force them earlier via
  *Run workflow* with `include_heavy` or an explicit `repos` list.
- **Health**: the daily Discord pulse and the dashboard fall back to the
  `Repo Mirror` run history for any repo without its own sync workflow; failed
  syncs also alert Discord immediately.
- Archived repos and the skip list are excluded automatically; empty failures
  never block the remaining repos' sync (the run goes red only if ≥ 1 selected
  repo actually failed).

All credentials are injected at runtime via Actions secrets
(`GH_DASHBOARD_TOKEN`, `DISCORD_WEBHOOK_URL`, `GDRIVE_RCLONE_OAUTH`,
`GDRIVE_SERVICE_ACCOUNT`, `CI_UNLOCK_TOKEN`, `PILOT_MONITOR_*`) and variables
(`GDRIVE_SHEET_ID`, `GDRIVE_FOLDER_ID`, `GDRIVE_TEAM_DRIVE`, `MIRROR_SKIP`) -
nothing sensitive is stored in code.
