# SyllabAI Ops

Automation workspace for the [SyllabAI](https://github.com/SyllabAI) project.

| Workflow | What it does |
|---|---|
| `google-sheet-dashboard.yml` | Refreshes the project & sync dashboard (a private Google Sheet) daily at 08:43 Asia/Dhaka |
| `discord-digest.yml` | Posts the daily project pulse to Discord (~09:07 Asia/Dhaka): commits in 24h + Drive-sync health |
| `keepalive.yml` | Monthly no-op commit so GitHub never auto-disables the schedules (60-day inactivity rule) |

**Why is this repo public?** GitHub Actions minutes are free and unlimited for
public repositories. Keeping scheduled automation here leaves the private
repos' included minutes for real CI.

All credentials are injected at runtime via Actions secrets
(`GH_DASHBOARD_TOKEN`, `DISCORD_WEBHOOK_URL`, `GDRIVE_RCLONE_OAUTH`) and
variables (`GDRIVE_SHEET_ID`, `GDRIVE_FOLDER_ID`) - nothing sensitive is
stored in code.
