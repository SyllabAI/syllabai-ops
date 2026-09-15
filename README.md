# SyllabAI Ops

Automation workspace for the [SyllabAI](https://github.com/SyllabAI) project.

| Workflow | What it does |
|---|---|
| `repo-mirror.yml` | Mirrors every SyllabAI repo's default branch to Google Drive — light lane every 30 min (repos ≤ 500 MB), heavy lane Saturdays 02:23 UTC (all repos, incl. multi-GB ones) + on-demand dispatch |
| `google-sheet-dashboard.yml` | Refreshes the project & sync dashboard (a private Google Sheet) daily at 08:43 Asia/Dhaka |
| `discord-digest.yml` | Posts the daily project pulse to Discord (~09:07 Asia/Dhaka): commits in 24h + Drive-sync health |
| `keepalive.yml` | Monthly no-op commit so GitHub never auto-disables the schedules (60-day inactivity rule) |

**Why is this repo public?** GitHub Actions minutes are free and unlimited for
public repositories. Keeping scheduled automation here leaves the private
repos' included minutes for real CI.

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
`GDRIVE_SERVICE_ACCOUNT`) and variables (`GDRIVE_SHEET_ID`, `GDRIVE_FOLDER_ID`,
`GDRIVE_TEAM_DRIVE`, `MIRROR_SKIP`) - nothing sensitive is stored in code.
