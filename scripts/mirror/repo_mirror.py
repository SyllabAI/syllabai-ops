#!/usr/bin/env python3
"""SyllabAI central repo mirror — all repos -> Google Drive.

Runs in the public syllabai-ops repo (free Actions minutes) and replaces the
per-repo google-drive-sync workflows that used to bill every push on private
repos. One scheduled job mirrors every repository's default branch to
`gdrive:SyllabAI-GitHub/<repo>` (same layout the per-repo workflows used, so
existing Drive folders keep working).

Two cadence lanes (see .github/workflows/repo-mirror.yml):
  light  - every 30 min; repos up to MIRROR_HEAVY_MB (default 500 MB)
  heavy  - weekly cron lane (or manual dispatch); every repo, including
           multi-GB ones (e.g. syllabai-pastpapers at ~4 GB)

Env:
  GH_TOKEN        GitHub PAT with read access to all SyllabAI repos
                  (secret GH_DASHBOARD_TOKEN)
  OAUTH_BLOCK     rclone config block - secret GDRIVE_RCLONE_OAUTH (preferred)
  SA_JSON         service-account JSON - secret GDRIVE_SERVICE_ACCOUNT (fallback;
                  requires a Workspace shared drive)
  FOLDER_ID       optional Drive folder id to pin the destination (vars)
  TEAM_DRIVE      optional shared-drive id (vars, wins over FOLDER_ID)
  MIRROR_REPOS    optional comma list - mirror exactly these, ignore tiers
  MIRROR_SKIP     optional comma list (default "syllabai-ops,syllabai-resources";
                  ops is this repo itself, resources keeps its own push-triggered
                  sync on a free public repo)
  MIRROR_HEAVY_MB heavy-tier threshold in MB (default 500)
  INCLUDE_HEAVY   "true" forces the heavy set into this run (dispatch input)
  EVENT_SCHEDULE  raw cron of the triggering schedule event (set by workflow);
                  equal to WEEKLY_CRON means the heavy lane
  WEEKLY_CRON     the workflow's weekly cron string (default "23 2 * * 6")

Flags:
  --dry-run       enumerate + resolve + plan, but clone/sync nothing.
                  Exits 0 regardless of failures (verification mode).

Failures: nonzero exit iff at least one selected repo failed to clone or sync.
Per-repo results land in $GITHUB_STEP_SUMMARY and /tmp/mirror_results.json;
failed repos also in /tmp/mirror_failures.txt (read by the Discord alert step).

Stdlib only. No credentials are ever placed in argv.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GH_TOKEN = os.environ.get("GH_TOKEN", "")
OAUTH_BLOCK = os.environ.get("OAUTH_BLOCK", "")
SA_JSON = os.environ.get("SA_JSON", "")
FOLDER_ID = os.environ.get("FOLDER_ID", "")
TEAM_DRIVE = os.environ.get("TEAM_DRIVE", "")
MIRROR_REPOS = [x.strip() for x in os.environ.get("MIRROR_REPOS", "").split(",") if x.strip()]
MIRROR_SKIP = [x.strip() for x in os.environ.get("MIRROR_SKIP", "syllabai-ops,syllabai-resources").split(",") if x.strip()]
MIRROR_HEAVY_MB = int(os.environ.get("MIRROR_HEAVY_MB", "500"))
INCLUDE_HEAVY = os.environ.get("INCLUDE_HEAVY", "").strip().lower() in ("1", "true", "yes")
EVENT_SCHEDULE = os.environ.get("EVENT_SCHEDULE", "")
WEEKLY_CRON = os.environ.get("WEEKLY_CRON", "23 2 * * 6")

SUMMARY_PATH = os.environ.get("GITHUB_STEP_SUMMARY", "")
RESULTS_JSON = "/tmp/mirror_results.json"
FAILURES_TXT = "/tmp/mirror_failures.txt"

RCLONE_TIMEOUT = 60 * 60          # one huge repo may legitimately take a while
CLONE_TIMEOUT = 30 * 60
EXCLUDES = ["--exclude", ".git/**", "--exclude", "node_modules/**",
            "--exclude", "__pycache__/**", "--exclude", ".venv/**",
            "--exclude", "venv/**", "--exclude", "*.pyc"]

USER_AGENT = "syllabai-repo-mirror"


def gh(path, params=None):
    """GitHub GET with retry; returns None after 3 failed attempts."""
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read().decode()
                return json.loads(data) if data else None
        except Exception as e:
            if attempt == 2:
                print(f"::warning::{path} failed: {e}")
                return None
            time.sleep(2 ** (attempt + 1))


def gh_page(path, params=None):
    """Yield every item of a paginated collection."""
    page = 1
    while True:
        p = dict(params or {})
        p["page"] = page
        batch = gh(path, p)
        if not isinstance(batch, list) or not batch:
            return
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def heavy_lane() -> bool:
    if INCLUDE_HEAVY:
        return True
    return bool(EVENT_SCHEDULE) and EVENT_SCHEDULE.strip() == WEEKLY_CRON.strip()


def select_repos():
    """Return (selected, deferred) repo dicts + lane annotation."""
    all_repos = list(gh_page("/user/repos", {
        "per_page": 100, "sort": "pushed", "visibility": "all",
        "affiliation": "owner"}))
    by_name = {r["full_name"]: r for r in all_repos}
    heavy = heavy_lane()

    if MIRROR_REPOS:
        missing = [n for n in MIRROR_REPOS if n not in by_name]
        if missing:
            print(f"::error::MIRROR_REPOS not found or not owned by this account: {missing}")
        picked = [by_name[n] for n in MIRROR_REPOS if n in by_name]
        return [(r, "explicit") for r in picked], []

    selected, deferred = [], []
    for r in sorted(all_repos, key=lambda x: x["full_name"]):
        full, name = r["full_name"], r["name"]
        if r.get("archived"):
            print(f"[skip] {full}: archived")
            continue
        if name in MIRROR_SKIP or full in MIRROR_SKIP:
            print(f"[skip] {full}: skip list")
            continue
        mb = (r.get("size") or 0) / 1024.0
        if not heavy and mb > MIRROR_HEAVY_MB:
            print(f"[defer] {full}: {mb:.0f} MB > {MIRROR_HEAVY_MB} MB heavy threshold"
                  f" - weekly lane / dispatch only")
            deferred.append((r, "heavy (weekly lane)"))
            continue
        selected.append((r, "heavy" if heavy else "light"))
    return selected, deferred


def rclone_env(workdir: str) -> tuple[str, str] | None:
    """Write the rclone config; return (env, mode) or None when no credential."""
    conf = os.path.join(workdir, "rclone.conf")
    os.chmod(workdir, 0o700)
    if OAUTH_BLOCK.strip():
        with open(conf, "w") as f:
            f.write(OAUTH_BLOCK if OAUTH_BLOCK.endswith("\n") else OAUTH_BLOCK + "\n")
        return {"RCLONE_CONFIG": conf}, "oauth"
    if SA_JSON.strip():
        key = os.path.join(workdir, "sa.json")
        with open(key, "w") as f:
            f.write(SA_JSON)
        os.chmod(key, 0o600)
        with open(conf, "w") as f:
            f.write("[gdrive]\n"
                    "type = drive\n"
                    "scope = drive\n"
                    f"service_account_file = {key}\n")
        return {"RCLONE_CONFIG": conf}, "service-account"
    return None


def dest_for(repo_name: str) -> tuple[str, list[str]]:
    """Same destination resolution the retired per-repo workflows used."""
    if TEAM_DRIVE.strip():
        return f"gdrive:{repo_name}", ["--drive-team-drive", TEAM_DRIVE.strip()]
    if FOLDER_ID.strip():
        return f"gdrive:{repo_name}", ["--drive-root-folder-id", FOLDER_ID.strip()]
    return f"gdrive:SyllabAI-GitHub/{repo_name}", []


def clone(repo_full: str, default_branch: str, workdir: str) -> tuple[bool, str]:
    """Shallow-clone the default branch. Token stays in the env: the helper
    string only REFERENCES $MIRROR_TOKEN, so it never appears in argv."""
    helper = ('!f() { printf "username=x-access-token\\npassword=%s\\n" '
              '"$MIRROR_TOKEN"; }; f')
    cmd = ["git", "-c", f"credential.helper={helper}",
           "clone", "--depth", "1", "--single-branch",
           "--branch", default_branch or "HEAD", "--quiet",
           f"https://github.com/{repo_full}.git", workdir]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT)
        if p.returncode == 0:
            return True, ""
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        return False, tail[-1][:160] if tail else f"git exit {p.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"clone timed out after {CLONE_TIMEOUT // 60} min"
    except Exception as e:
        return False, str(e)[:160]


def rclone_sync(src: str, dest: str, extra: list[str],
                renv: dict, log: str) -> tuple[bool, str]:
    """rclone sync with the quota-hint + shared-with-me retry of the old workflow."""
    base = ["rclone", "sync", src, dest] + EXCLUDES + extra + [
        "--transfers", "8", "--checkers", "16", "--fast-list",
        "--stats-one-line", "--stats", "0", "-v"]
    with open(log, "w") as lf:
        try:
            p = subprocess.run(base, env={**os.environ, **renv},
                               stdout=lf, stderr=subprocess.STDOUT,
                               timeout=RCLONE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"sync timed out after {RCLONE_TIMEOUT // 60} min"
    if p.returncode == 0:
        return True, _last_stats(log)

    text = open(log).read() if os.path.exists(log) else ""
    if "storage quota" in text:
        print("::error::Google blocks service-account uploads to personal My Drive. "
              "Fix: (A) Workspace Shared Drive -> SA as Content Manager + var "
              "GDRIVE_TEAM_DRIVE; or (B) OAuth -> secret GDRIVE_RCLONE_OAUTH.")
    if not extra:  # only the by-name fallback has a shared-with-me retry
        with open(log, "a") as lf:
            lf.write("\n-- retrying with --drive-shared-with-me --\n")
            try:
                p2 = subprocess.run(base + ["--drive-shared-with-me"],
                                    env={**os.environ, **renv},
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    timeout=RCLONE_TIMEOUT)
            except subprocess.TimeoutExpired:
                return False, f"sync (retry) timed out after {RCLONE_TIMEOUT // 60} min"
        if p2.returncode == 0:
            return True, _last_stats(log) + " (shared-with-me)"
    tail = [ln for ln in text.strip().splitlines() if ln.strip()]
    return False, tail[-1][:160] if tail else f"rclone exit {p.returncode}"


def _last_stats(log: str) -> str:
    """Pull the final one-line rclone stats for the summary table."""
    try:
        lines = [ln for ln in open(log).read().splitlines() if ln.strip()]
        for ln in reversed(lines):
            if "Transferred:" in ln or "There was nothing to transfer" in ln:
                return re.sub(r"\s+", " ", ln.strip())[:120]
        return lines[-1][:120] if lines else "ok"
    except Exception:
        return "ok"


def main() -> int:
    dry = "--dry-run" in sys.argv
    if not GH_TOKEN:
        print("::error::GH_TOKEN missing")
        return 1
    if not os.environ.get("MIRROR_TOKEN"):
        os.environ["MIRROR_TOKEN"] = GH_TOKEN
    if not dry and shutil.which("rclone") is None:
        print("::error::rclone not installed")
        return 1

    lane = "heavy" if heavy_lane() else "light"
    print(f"[plan] lane={lane} heavy_mb={MIRROR_HEAVY_MB} skip={MIRROR_SKIP}"
          f" explicit={MIRROR_REPOS or '-'}")
    selected, deferred = select_repos()
    if not selected and not deferred:
        print("::warning::no repositories selected - nothing to do")
        return 0
    for r, tag in selected + deferred:
        print(f"[plan] {r['full_name']:32} {(r.get('size') or 0) / 1024:8.1f} MB  ({tag})")
    if dry:
        print("[dry-run] plan only - no clone, no sync")
        return 0

    renv_pack = rclone_env(tempfile.mkdtemp(prefix="mirror-conf-"))
    if renv_pack is None:
        print("::notice::No Google credential found (set secret GDRIVE_RCLONE_OAUTH "
              "or GDRIVE_SERVICE_ACCOUNT) - mirror skipped.")
        return 0
    renv, auth_mode = renv_pack
    print(f"[auth] rclone mode: {auth_mode}")

    rows, failures = [], []
    for r, tag in selected:
        full, name = r["full_name"], r["name"]
        mb = (r.get("size") or 0) / 1024.0
        workdir = tempfile.mkdtemp(prefix=f"mirror-{name}-")
        t0 = time.monotonic()
        ok, err = clone(full, r.get("default_branch") or "main", workdir)
        detail = ""
        if ok:
            dest, extra = dest_for(name)
            ok, detail = rclone_sync(workdir, dest, extra, renv,
                                     os.path.join(workdir, "rclone.log"))
        else:
            detail = err
        shutil.rmtree(workdir, ignore_errors=True)
        mins = (time.monotonic() - t0) / 60
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {full} ({mb:.1f} MB, {mins:.1f} min) - {detail or 'synced'}")
        rows.append({"repo": full, "mb": round(mb, 1), "lane": tag,
                     "status": status, "detail": detail,
                     "minutes": round(mins, 1)})
        if not ok:
            failures.append(f"{full} — {detail}")

    _write_summary(rows, lane, auth_mode)
    with open(RESULTS_JSON, "w") as f:
        json.dump({"lane": lane, "auth": auth_mode,
                   "finished": datetime.now(timezone.utc).isoformat(),
                   "results": rows}, f, indent=1)
    with open(FAILURES_TXT, "w") as f:
        f.write("\n".join(failures))
    print(f"[done] {len(rows) - len(failures)}/{len(rows)} mirrored, "
          f"{len(deferred)} deferred to the heavy lane")
    return 1 if failures else 0


def _write_summary(rows: list, lane: str, auth_mode: str) -> None:
    if not SUMMARY_PATH:
        return
    lines = [f"### Repo Mirror — {lane} lane (auth: {auth_mode})", "",
             "| Repo | Size | Lane | Result | Detail |",
             "|---|---|---|---|---|"]
    for r in rows:
        icon = "✅" if r["status"] == "OK" else "❌"
        lines.append(f"| {icon} `{r['repo']}` | {r['mb']} MB | {r['lane']} "
                     f"| {r['status']} ({r['minutes']} min) "
                     f"| {r['detail'].replace('|', '/')} |")
    with open(SUMMARY_PATH, "a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    sys.exit(main())
