#!/usr/bin/env python3
"""SyllabAI Google Sheet dashboard updater (v2).

Gathers repo metadata, default-branch commits, Google Drive Sync run health
(GitHub API) and Drive folder statistics (Drive API), then writes them to a
Google Sheet with tabs: Overview, Commits, Sync Runs, Activity, State, Drive Growth.

v2 additions: sync-health verdicts (emoji), Needs-Attention block, commit
activity pulse (7d/30d + 8-week sparkline), Activity heatmap tab, top
contributors, open PRs/issues, Drive folder links, Drive growth deltas
(hidden State tab stores the per-run snapshot), freshness stamp.

Runs inside GitHub Actions (SyllabAI/syllabai). Stdlib only.

Env:
  GH_TOKEN      GitHub PAT (repo read + Actions read across SyllabAI repos)
  OAUTH_BLOCK   the GDRIVE_RCLONE_OAUTH secret ([gdrive] rclone config block)
  SHEET_ID      target spreadsheet id (repo variable GDRIVE_SHEET_ID)
  FOLDER_ID     SyllabAI-GitHub Drive folder id (repo variable GDRIVE_FOLDER_ID)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

GH_ORG = os.environ.get("GH_ORG", "SyllabAI")
SHEET_ID = os.environ.get("SHEET_ID", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
OAUTH_BLOCK = os.environ.get("OAUTH_BLOCK", "")
FOLDER_ID = os.environ.get("FOLDER_ID", "")
MAX_COMMIT_ROWS = 5000
MAX_DRIVE_FILES = 5000
MAX_GROWTH_ROWS = 1500
INC_PAGES = 2     # commit pages (100/page) per repo per normal run
FULL_PAGES = 20   # when the Commits tab starts empty (first run)
FAILED = {"failure", "timed_out", "startup_failure", "action_required"}

NOW = datetime.now(timezone.utc).replace(tzinfo=None)
NOW_STR = NOW.strftime("%Y-%m-%d %H:%M")

ROLES = {
    "syllabai": "Master project pack — spec, ADRs, decisions, trackers",
    "syllabai-core": "Backend modular monolith — Java 25 · Spring Boot 4.1 · Render",
    "syllabai-web": "Frontend — Next.js 16 · React 19 · Vercel",
    "syllabai-parser": "Content pipeline — offline document parsing (Java/Python)",
    "syllabai-pastpapers": "Canonical QP/MS corpus (official assessment materials)",
    "Past-Papers": "Edexcel IGCSE Chemistry QP/MS + GLM-OCR markdown",
    "syllabai-resources": "Revision notes, textbooks, specification corpus",
}


def die(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def http(method, url, data=None, headers=None, timeout=60, retries=2):
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                if raw.strip() and not raw.lstrip().startswith(("{", "[")):
                    die(f"{method} {url} -> non-JSON response (blocked?): {raw[:150]}")
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            die(f"{method} {url} -> {e}")


def gh(path, params=None, fatal=True):
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    st, data = http("GET", url, headers={
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "syllabai-dashboard"})
    if st != 200:
        if fatal:
            die(f"GitHub GET {path} -> {st}: {json.dumps(data)[:200]}")
        return None
    return data


def parse_oauth_block(text):
    cid = sec = tok = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("client_id"):
            cid = s.split("=", 1)[1].strip()
        elif s.startswith("client_secret"):
            sec = s.split("=", 1)[1].strip()
        elif s.startswith("token = "):
            tok = json.loads(s.split("= ", 1)[1].strip())
    if not (cid and sec and tok and tok.get("refresh_token")):
        die("OAUTH_BLOCK parse failed — is GDRIVE_RCLONE_OAUTH set?")
    return cid, sec, tok["refresh_token"]


def google_token(cid, sec, rtok):
    d = urllib.parse.urlencode({"client_id": cid, "client_secret": sec,
                                "refresh_token": rtok, "grant_type": "refresh_token"}).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    "https://oauth2.googleapis.com/token", data=d, method="POST"), timeout=30) as r:
                return json.loads(r.read().decode())["access_token"]
        except Exception as e:
            if attempt == 2:
                die(f"Google token refresh failed: {e}")
            time.sleep(2 ** (attempt + 1))


def gdrive_get(path, token):
    return http("GET", f"https://www.googleapis.com/drive/v3{path}",
                headers={"Authorization": f"Bearer {token}"})


def sheet_clear(tab, token):
    rng = urllib.parse.quote(f"{tab}!A1:Z10000", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{rng}:clear"
    st, data = http("POST", url, headers={"Authorization": f"Bearer {token}"})
    if st != 200:
        die(f"sheet_clear {tab} -> {st}: {json.dumps(data)[:200]}")


def sheet_put(tab, rows, token):
    # single-range write form (values/{range}?valueInputOption=...) gets served an HTML
    # block page from datacenter IPs - use values:batchUpdate instead (verified working)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate"
    payload = {"valueInputOption": "USER_ENTERED",
               "data": [{"range": f"{tab}!A1", "values": rows}]}
    st, data = http("POST", url, data=payload, headers={"Authorization": f"Bearer {token}"})
    if st not in (200, 201):
        die(f"sheet_put {tab} -> {st}: {json.dumps(data)[:200]}")


def sheet_get(tab, token, cols="A2:E"):
    rng = urllib.parse.quote(f"{tab}!{cols}", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{rng}"
    st, data = http("GET", url, headers={"Authorization": f"Bearer {token}"})
    return data.get("values", []) if st == 200 else []


def ensure_tabs(token):
    """Self-healing: create Activity/State/Drive Growth tabs if missing."""
    st, data = http("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
                           "?fields=sheets.properties.title",
                    headers={"Authorization": f"Bearer {token}"})
    titles = {s["properties"].get("title") for s in data.get("sheets", [])} if st == 200 else set()
    missing = [t for t in ("Activity", "State", "Drive Growth") if t not in titles]
    if missing:
        http("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
             data={"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]},
             headers={"Authorization": f"Bearer {token}"})
        print(f"[OK] created tabs: {missing}")


def safe(v, maxlen=500):
    s = str(v).replace("\r", " ").replace("\n", " | ")
    if s.startswith(("=", "+", "@")):
        s = "'" + s
    return s[:maxlen]


def iso_utc(s):
    return (s or "").replace("T", " ").replace("Z", "")[:19]


def sha_of_cell(cell):
    """Extract full sha from =HYPERLINK(\"...commit/<sha>\",...) or plain text."""
    c = str(cell)
    if c.startswith("=HYPERLINK("):
        parts = c.split('"')
        if len(parts) > 1:
            return parts[1].rstrip("/").split("/")[-1]
    return c.strip()


def parse_ts(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def age_hours(s):
    d = parse_ts(s)
    return (NOW - d).total_seconds() / 3600 if d else None


def drive_children(folder_id, token):
    out, page_token = [], None
    while True:
        params = {"q": f"'{folder_id}' in parents and trashed=false",
                  "fields": "nextPageToken, files(id,name,size,mimeType)", "pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        st, data = gdrive_get("/files?" + urllib.parse.urlencode(params), token)
        if st != 200:
            break
        out += data.get("files", [])
        page_token = data.get("nextPageToken")
        if not page_token or len(out) >= MAX_DRIVE_FILES:
            break
    return out


def drive_stats(folder_id, token):
    files, size, stack = 0, 0, [folder_id]
    while stack and files < MAX_DRIVE_FILES:
        fid = stack.pop()
        for it in drive_children(fid, token):
            if it.get("mimeType") == "application/vnd.google-apps.folder":
                stack.append(it["id"])
            else:
                files += 1
                size += int(it.get("size", 0))
    return files, round(size / 1e6, 1)


def duration_min(run):
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.strptime(run.get("run_started_at") or run["created_at"], fmt)
        end = datetime.strptime(run.get("updated_at"), fmt)
        return round((end - start).total_seconds() / 60, 1)
    except Exception:
        return ""


def health_of(runs, fresh_h=48, stale_h=168):
    """Emoji verdict from recent sync runs - the repo's own "Google Drive Sync"
    workflow, or the central Repo Mirror in syllabai-ops (30-min cadence,
    judged with the tighter 2h/24h thresholds)."""
    if not runs:
        return "⚪ no runs"
    r0 = runs[0]
    if r0.get("status") in ("in_progress", "queued"):
        return "⏳ running"
    if (r0.get("conclusion") or "") in FAILED:
        return "🔴 failed"
    ok = next((r for r in runs if r.get("conclusion") == "success"), None)
    if not ok:
        return "🔴 never synced"
    h = age_hours(iso_utc(ok.get("run_started_at") or ok["created_at"]))
    if h is None:
        return "🟡 unknown"
    if h <= fresh_h:
        return "🟢 fresh"
    if h <= stale_h:
        return "🟡 stale"
    return "🔴 stale"


def spark(weeks):
    """8-week commit trend as an in-cell column sparkline (oldest -> newest)."""
    if not any(weeks):
        return "-"
    vals = ",".join(str(n) for n in reversed(weeks))
    return f'=SPARKLINE({{{vals}}},{{"charttype","column";"color","#1a73e8"}})'


def open_prs(full):
    data = gh("/search/issues", {"q": f"repo:{full} is:pr state:open", "per_page": 1}, fatal=False)
    if data and isinstance(data, dict) and "total_count" in data:
        return data["total_count"]
    return None


def main():
    for name in ("SHEET_ID", "GH_TOKEN", "OAUTH_BLOCK"):
        if not os.environ.get(name):
            die(f"missing env {name}")

    cid, sec, rtok = parse_oauth_block(OAUTH_BLOCK)
    gtoken = google_token(cid, sec, rtok)
    print("[OK] Google token refreshed")
    ensure_tabs(gtoken)

    # /user/repos returns public + private for the token's own account;
    # /users/{user}/repos returns public only
    repos = gh("/user/repos", {"per_page": 100, "sort": "pushed",
                               "visibility": "all", "affiliation": "owner"})
    print(f"[OK] {len(repos)} repos fetched")

    # ---- commits: merge new into existing log ----
    existing = sheet_get("Commits", gtoken)
    seen = {sha_of_cell(r[3]) for r in existing if len(r) > 3}
    full_mode = len(existing) < 50
    pages = FULL_PAGES if full_mode else INC_PAGES
    new_rows = []
    for repo in repos:
        full = repo["full_name"]
        for page in range(1, pages + 1):
            batch = gh(f"/repos/{full}/commits", {"per_page": 100, "page": page})
            if not batch:
                break
            for c in batch:
                sha = c["sha"]
                if sha in seen:
                    continue
                seen.add(sha)
                author = (c.get("commit", {}).get("author", {}) or {}).get("name", "unknown")
                msg = c["commit"]["message"].splitlines()[0]
                new_rows.append([iso_utc(c["commit"]["author"]["date"]), repo["name"],
                                 safe(author, 80), f'=HYPERLINK("{c["html_url"]}","{sha[:7]}")',
                                 safe(msg, 300)])
            if len(batch) < 100:
                break
    all_rows = new_rows + [list(r) for r in existing]
    all_rows.sort(key=lambda r: str(r[0]), reverse=True)
    all_rows = all_rows[:MAX_COMMIT_ROWS]
    print(f"[OK] commits: {len(existing)} existing, +{len(new_rows)} new -> {len(all_rows)} rows")

    # ---- commit analytics (7d/30d/8-week buckets + contributors) ----
    seven_d, thirty_d, weeks_by_repo, contrib = {}, {}, {}, {}
    for r in all_rows:
        d = parse_ts(r[0])
        if not d or len(r) < 3:
            continue
        age = (NOW - d).total_seconds() / 86400
        name, author = str(r[1]), str(r[2])
        if age <= 7:
            seven_d[name] = seven_d.get(name, 0) + 1
        if age <= 30:
            thirty_d[name] = thirty_d.get(name, 0) + 1
            c0 = contrib.setdefault(author, {"n": 0, "repos": set()})
            c0["n"] += 1
            c0["repos"].add(name)
        if 0 <= age < 56:
            w = weeks_by_repo.setdefault(name, [0] * 8)
            w[(NOW - d).days // 7] += 1
    print(f"[OK] activity: {len(contrib)} contributors in last 30d")

    # ---- sync runs (last 25 per repo) + health inputs ----
    # Central mirror (SyllabAI/syllabai-ops): repos without their own
    # "Google Drive Sync" workflow get their health from Repo Mirror runs.
    central_data = gh("/repos/SyllabAI/syllabai-ops/actions/runs", {"per_page": 30})
    central_runs = [r for r in (central_data or {}).get("workflow_runs", [])
                    if r.get("name") == "Repo Mirror"]
    run_rows, runs_by_repo, central_only = [], {}, set()
    for repo in repos:
        full = repo["full_name"]
        # Own sync rows/health only when the repo still HAS an active own sync
        # workflow - retired ones (core's deleted google-drive-sync) leave run
        # history behind that must not read as current health.
        wf = gh(f"/repos/{full}/actions/workflows") or {}
        own = any(w.get("name") == "Google Drive Sync" and w.get("state") == "active"
                  for w in wf.get("workflows", []))
        runs = []
        if own:
            data = gh(f"/repos/{full}/actions/runs", {"per_page": 30})
            runs = [r for r in data.get("workflow_runs", []) if r.get("name") == "Google Drive Sync"]
        if runs:
            runs_by_repo[full] = runs
        else:
            runs_by_repo[full] = central_runs
            central_only.add(full)
        for r in runs[:25]:
            run_rows.append([repo["name"], r["status"], r.get("conclusion") or "-",
                             r.get("head_branch", ""), r.get("event", ""),
                             iso_utc(r.get("run_started_at") or r["created_at"]),
                             duration_min(r), f'=HYPERLINK("{r["html_url"]}","open")'])
    for r in central_runs[:25]:
        run_rows.append(["central — syllabai-ops (Repo Mirror)", r["status"],
                         r.get("conclusion") or "-",
                         r.get("head_branch", ""), r.get("event", ""),
                         iso_utc(r.get("run_started_at") or r["created_at"]),
                         duration_min(r), f'=HYPERLINK("{r["html_url"]}","open")'])
    print(f"[OK] sync runs: {len(run_rows)} rows ({len(central_only)} repos on central mirror)")

    # ---- drive stats per repo folder + growth deltas vs State tab ----
    drive_map = {}
    if FOLDER_ID:
        for it in drive_children(FOLDER_ID, gtoken):
            if it.get("mimeType") == "application/vnd.google-apps.folder":
                drive_map[it["name"]] = it["id"]
    else:
        print("[WARN] FOLDER_ID not set — skipping Drive stats")

    prev_state = {}
    for r in sheet_get("State", gtoken, cols="A2:D"):
        try:
            prev_state[str(r[0])] = (int(r[1]), float(r[2]))
        except Exception:
            continue
    new_state, growth_new = [], []
    for repo in repos:
        name = repo["name"]
        files, mb = drive_stats(drive_map[name], gtoken) if name in drive_map else (0, 0.0)
        new_state.append([name, files, mb, NOW_STR])
        prev = prev_state.get(name)
        if prev:
            df, dm = files - prev[0], round(mb - prev[1], 1)
            delta = "no change" if (df == 0 and dm == 0) else f"{df:+} files / {dm:+,.1f} MB"
            growth_new.append([NOW_STR, name, files, mb, df, dm])
        else:
            delta = "first snapshot"
            growth_new.append([NOW_STR, name, files, mb, "", ""])
    growth_existing = sheet_get("Drive Growth", gtoken, cols="A2:F")
    growth_rows = growth_new + [list(r) for r in growth_existing]
    growth_rows = growth_rows[:MAX_GROWTH_ROWS]
    print(f"[OK] drive stats: {len(new_state)} folders, growth log {len(growth_rows)} rows")

    # ---- overview ----
    ds_map = {r[0]: (r[1], r[2]) for r in new_state}
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    slug = os.environ.get("GITHUB_REPOSITORY", f"{GH_ORG}/syllabai")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_link = (f'=HYPERLINK("{server}/{slug}/actions/runs/{run_id}","run #{run_id}")'
                if run_id else "manual run")
    ov_rows, attention = [], []
    for repo in sorted(repos, key=lambda r: r["name"]):
        full, name = repo["full_name"], repo["name"]
        lc = gh(f"/repos/{full}/commits", {"per_page": 1})
        lc = lc[0] if isinstance(lc, list) and lc else None
        files, mb = ds_map.get(name, ("-", "-"))
        role = ROLES.get(name) or repo.get("description") or ""
        lc_time = lc_msg = ""
        if lc:
            lc_time = iso_utc(lc["commit"]["author"]["date"])
            lc_msg = safe(lc["commit"]["message"].splitlines()[0], 200)
        runs = runs_by_repo.get(full, [])
        health = health_of(runs, 2, 24) if full in central_only else health_of(runs)
        s_status, s_time, s_url = "no runs yet", "-", ""
        if runs:
            r0 = runs[0]
            s_status = r0["status"] if r0["status"] != "completed" else (r0.get("conclusion") or "-")
            s_time = iso_utc(r0.get("run_started_at") or r0["created_at"])
            s_url = r0["html_url"]
        prs = open_prs(full)
        issues = repo.get("open_issues_count", 0)
        issues = max(0, issues - prs) if prs is not None else issues

        # growth delta for this repo (from the growth rows just built)
        g0 = next((g for g in growth_new if g[1] == name), None)
        if g0 is None:
            delta = "-"
        elif g0[4] == "":
            delta = "first snapshot"
        elif g0[4] == 0 and g0[5] == 0:
            delta = "no change"
        else:
            delta = f"{g0[4]:+} files / {g0[5]:+,.1f} MB"

        ov_rows.append([
            name, f'=HYPERLINK("{repo["html_url"]}","open")', safe(role, 150),
            "private" if repo["private"] else "public",
            repo.get("language") or "-", repo.get("default_branch") or "main",
            seven_d.get(name, 0), thirty_d.get(name, 0),
            spark(weeks_by_repo.get(name, [0] * 8)),
            lc_time, lc_msg,
            prs if prs is not None else "-", issues,
            health, s_status,
            f'=HYPERLINK("{s_url}","open")' if s_url else "-",
            s_time,
            f'=HYPERLINK("https://drive.google.com/drive/folders/{drive_map[name]}","open")'
            if name in drive_map else "-",
            files, mb, safe(delta, 40),
        ])
        if health.startswith("🔴"):
            why = f"last sync: {s_status} at {s_time} UTC" if s_time != "-" else "never synced"
            attention.append([f"{health} {name} — {why}",
                              f'=HYPERLINK("{s_url}","open run")' if s_url else ""])
        if name not in drive_map:
            attention.append([f"📁 {name} — no Drive folder found in SyllabAI-GitHub", ""])

    ov_head = [["🚀 SyllabAI — GitHub → Google Drive Dashboard"],
               [f"Updated {NOW_STR} UTC · {run_link} · "
                f"auto-refresh: every 6 h and on every push to syllabai main"],
               [""]]
    ov_header = ["Repo", "GitHub", "Role", "Visibility", "Language", "Branch",
                 "Commits 7d", "Commits 30d", "Trend (8 wks)",
                 "Last commit (UTC)", "Last commit message",
                 "Open PRs", "Open issues",
                 "Health", "Last sync", "Last sync run", "Last sync time (UTC)",
                 "Drive folder", "Drive files", "Drive size (MB)", "Δ since last run"]
    if attention:
        ov_tail = [[], ["⚠ NEEDS ATTENTION"]] + attention
    else:
        ov_tail = [[], ["✅ All repos healthy — every Drive sync is green"]]
    print("[OK] overview rows built")

    # ---- activity tab ----
    week_labels = [(NOW - timedelta(days=7 * i + 6)).strftime("%b %d") for i in range(8)][::-1]
    act = [["Repo"] + week_labels + ["Total 8w"]]
    for repo in sorted(repos, key=lambda r: r["name"]):
        w = weeks_by_repo.get(repo["name"], [0] * 8)
        act.append([repo["name"]] + [n for n in reversed(w)] + [sum(w)])
    act += [[], ["Top contributors (last 30 days)"], ["Author", "Commits", "Repos"]]
    for a, v in sorted(contrib.items(), key=lambda kv: -kv[1]["n"])[:10]:
        act.append([a, v["n"], safe(", ".join(sorted(v["repos"])), 150)])

    # ---- write everything ----
    sheet_clear("Overview", gtoken)
    sheet_put("Overview", ov_head + [ov_header] + ov_rows + ov_tail, gtoken)
    sheet_clear("Sync Runs", gtoken)
    sheet_put("Sync Runs", [["Repo", "Status", "Conclusion", "Branch", "Event",
                             "Started (UTC)", "Duration (min)", "Run link"]] + run_rows, gtoken)
    sheet_clear("Commits", gtoken)
    sheet_put("Commits", [["Time (UTC)", "Repo", "Author", "SHA", "Message"]] + all_rows, gtoken)
    sheet_clear("Activity", gtoken)
    sheet_put("Activity", act, gtoken)
    sheet_clear("State", gtoken)
    sheet_put("State", [["Repo", "Files", "Size (MB)", "Updated (UTC)"]] + new_state, gtoken)
    sheet_clear("Drive Growth", gtoken)
    sheet_put("Drive Growth", [["Time (UTC)", "Repo", "Files", "Size (MB)",
                                "Δ files", "Δ MB"]] + growth_rows, gtoken)
    print(f"[DONE] dashboard refreshed — {len(ov_rows)} repos, {len(run_rows)} runs, "
          f"{len(all_rows)} commits, {len(contrib)} contributors")


if __name__ == "__main__":
    main()
