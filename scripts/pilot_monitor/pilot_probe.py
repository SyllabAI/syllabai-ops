#!/usr/bin/env python3
"""SyllabAI pilot monitor — the minimum operational monitoring (DEPLOYMENT.md §4).

Runs from GitHub Actions (web repo, cron) against the deployed production
surfaces over real HTTP. Zero new infrastructure: it reuses the existing
DISCORD_WEBHOOK_URL secret for reporting. Stdlib only.

Checks (the pilot runbook §4 minimum):
  health          backend /actuator/health — cold-start tolerant, records wake time
  cors            browser-origin preflight from the real web origin is allowed
  auth-401        bad-credential login fails as 401 (auth failure mode, not 5xx)
  teacher-guard   anonymous request to a teacher route is rejected 401
  web-bundle      deployed Vercel bundle contains the pilot UI markers
                  (subject-scoped practice, v1.1 cards, ConceptGraphView, 4CH1 UI)
  learner         monitor account: subjects + recommendations (policy pinned) + practice list
  scoping         unknown practice root is 404 (subject-scoped practice fix, f7adea7)
  missing-param   recommendations without rootId is 400 (5991187)
  contamination   if 4CH1 is activated: its practice list must not overlap any
                  other subject's list (cross-subject leakage; since the 2026-09-14
                  §10 topic mapping, 4CH1 legitimately SERVES its validated
                  topic-mapped questions — "zero served" is no longer the goal)
  teacher         if PILOT_TEACHER_* secrets exist: concept-graph read (edges > 0);
                  weekly/manual runs also re-verify idempotent activation (0 created)
  class-analytics  sprint-2 §2: class overview policy + honesty (unmeasured = null)
  marking-lane     sprint-2 §6/§7: the deterministic paper-grouped marking queue
                  (mark→next chain, state honesty), throughput counts and the
                  review-queue-v3 rank reasons — read-only
  smart-lesson     sprint-2 §8: Smart Lesson v2 policy + explainability shape
                  (action with reason code + evidence trace) on a servable topic
  weakness-targeting  sprint-2 §10: class-weakness options (policy id, explicit
                  known reasons only, no synthetic score field) — read-only
  kappa           structured-assessment κ status — N/A until T-C04 content ships

The weekly schedule adds the operator reminders (evidence export, κ, feedback
triage). Exit code 1 when any check fails, after the Discord report is posted.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BACKEND = os.environ.get("BACKEND_URL", "https://syllabai-core.onrender.com")
WEB = os.environ.get("WEB_URL", "https://syllabai-web.vercel.app")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
MONITOR_EMAIL = os.environ.get("MONITOR_EMAIL", "")
MONITOR_PASSWORD = os.environ.get("MONITOR_PASSWORD", "")
TEACHER_EMAIL = os.environ.get("TEACHER_EMAIL", "")
TEACHER_PASSWORD = os.environ.get("TEACHER_PASSWORD", "")
EVENT_NAME = os.environ.get("EVENT_NAME", "workflow_dispatch")
EVENT_SCHEDULE = os.environ.get("EVENT_SCHEDULE", "")
RUN_URL = os.environ.get("RUN_URL", "")

# Deployed-bundle markers, one per required pilot feature (session-58 verified
# against a local production build of web main ca01d9e+; sprint-2 §2 marker
# c3fb24d+ — goes red until the Vercel git-author block is resolved and the
# class-intelligence tab actually ships):
BUNDLE_MARKERS = {
    "subject-scoped practice (ca01d9e)": "No validated questions in this subject yet",
    "v1.1 recommendation cards (d82d303)": "Fix misconception",
    "Teacher ConceptGraphView (47a3085)": "The official 4CH1 specification tree",
    "4CH1 concept graph UI": "Re-verify 4CH1 seed",
    "teacher class intelligence (c3fb24d)": "Class intelligence",
}

UNKNOWN_ROOT = "00000000-0000-0000-0000-0000000000ff"

RESULTS: list[dict] = []


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None,
         timeout: int = 30) -> tuple[int, object, str]:
    """One HTTP call; returns (status, parsed-json-or-raw, error-or-'')."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        **(headers or {}),
        **({"Content-Type": "application/json"} if data else {}),
        "User-Agent": "syllabai-pilot-monitor/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), ""
            except json.JSONDecodeError:
                return r.status, raw, ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), ""
        except json.JSONDecodeError:
            return e.code, raw, ""
    except Exception as e:  # noqa: BLE001 — network failures must be reported, not crash
        return 0, None, f"{type(e).__name__}: {e}"


def record(name: str, ok: bool, detail: str, fail_note: str = "", warn: bool = False) -> None:
    RESULTS.append({"name": name, "ok": ok, "detail": detail,
                    "note": fail_note if not ok else "", "warn": warn and ok})
    mark = "OK " if ok and not warn else ("WARN" if ok else "FAIL")
    print(f"[{mark}] {name}: {detail}")


# ── 1. backend health (cold-start tolerant: up to ~5 min) ─────────────────────
def check_health() -> float:
    deadline = time.monotonic() + 300
    attempt, status, body, err = 0, 0, None, ""
    while time.monotonic() < deadline:
        attempt += 1
        status, body, err = http("GET", f"{BACKEND}/actuator/health", timeout=30)
        if status == 200 and isinstance(body, dict) and body.get("status") == "UP":
            wake = 300 - (deadline - time.monotonic())
            record("health", True, f"UP (attempt {attempt})")
            return wake
        time.sleep(15)
    record("health", False,
           f"not UP after {attempt} attempts (last {status or 'network'} {err or body})",
           "backend down or failing health checks — pilot blocker")
    return 0.0


# ── 2. CORS preflight from the real web origin ────────────────────────────────
def check_cors() -> None:
    status, body, err = http("OPTIONS", f"{BACKEND}/api/v1/auth/login", headers={
        "Origin": WEB,
        "Access-Control-Request-Method": "POST",
    })
    if status == 200:
        record("cors", True, f"preflight 200 from {WEB}")
    else:
        record("cors", False, f"preflight {status} {err or ''}",
               "browser sign-in from the deployed web origin would fail")


# ── 3. auth failure mode: bad login must be a clean 401 ───────────────────────
def check_auth_401() -> None:
    status, body, err = http("POST", f"{BACKEND}/api/v1/auth/login", body={
        "email": "definitely-not-a-user@syllabai.invalid",
        "password": "wrong-password-monitor-probe",
    })
    if status == 401:
        record("auth-401", True, "bad credentials rejected as 401")
    elif status >= 500:
        record("auth-401", False, f"bad credentials produced {status}",
               "auth failure mode is a 5xx — backend defect")
    else:
        record("auth-401", False, f"unexpected {status}", "auth path behaves abnormally")


# ── 4. RBAC: anonymous request to a teacher route must be 401 ─────────────────
def check_teacher_guard() -> None:
    status, _, _ = http("GET",
                        f"{BACKEND}/api/v1/teacher/concept-graph/edges?rootId={UNKNOWN_ROOT}")
    record("teacher-guard", status == 401,
           f"anonymous teacher-route access → {status}",
           "teacher routes are not authentication-guarded" if status != 401 else "")


# ── 5. deployed web bundle carries the pilot UI ───────────────────────────────
def check_web_bundle() -> None:
    status, body, err = http("GET", WEB + "/", timeout=30)
    if status != 200 or not isinstance(body, str):
        record("web-bundle", False, f"index {status or 'network'} {err}",
               "production web is not serving")
        return
    chunks = re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', body)
    if not chunks:
        record("web-bundle", False, "no chunk references in index",
               "production web bundle is not a Next build")
        return
    blob = ""
    for c in chunks:
        _, chunk, _ = http("GET", WEB + c, timeout=30)
        if isinstance(chunk, str):
            blob += chunk
    missing = {k: v for k, v in BUNDLE_MARKERS.items() if v not in blob}
    if missing:
        record("web-bundle", False,
               f"stale bundle — {len(chunks)} chunks scanned, missing: {', '.join(missing)}",
               "deployed web predates the pilot UI (subject selector / v1.1 cards / "
               "ConceptGraphView) — Vercel deploy of main required")
    else:
        record("web-bundle", True, f"all {len(BUNDLE_MARKERS)} pilot markers present "
                                   f"in {len(chunks)} chunks")


# ── 6-9. learner journey smoke + deployed-backend freshness + contamination ───
def check_learner() -> None:
    if not (MONITOR_EMAIL and MONITOR_PASSWORD):
        record("learner", False, "monitor credentials not configured",
               "PILOT_MONITOR_* secrets missing")
        return
    status, body, err = http("POST", f"{BACKEND}/api/v1/auth/login", body={
        "email": MONITOR_EMAIL, "password": MONITOR_PASSWORD})
    if status != 200 or not isinstance(body, dict):
        record("learner", False, f"monitor login {status} {err}",
               "monitor account broken — re-provision")
        return
    auth = {"Authorization": f"Bearer {body['accessToken']}"}

    # subjects
    status, subs, err = http("GET", f"{BACKEND}/api/v1/curriculum/subjects", headers=auth)
    if status != 200 or not isinstance(subs, list):
        record("learner", False, f"subjects {status} {err}", "curriculum read broken")
        return
    rooted = [s for s in subs if s.get("knowledgeNodeId")]
    record("learner", True, f"login + {len(subs)} subjects ({len(rooted)} with KG root)")

    # practice list + NBA on the first rooted subject THAT SERVES validated
    # questions (recovery 2026-09-13: since 4CH1 activation, subjects[0] is 4CH1
    # — honest-empty by design, and its 370-node subtree makes its NBA scan the
    # slowest call on the 0.1-CPU Render free tier; the learner loop the pilot
    # actually runs is the question-serving subject). Fall back to the first
    # rooted subject when none serves questions yet.
    if rooted:
        serving = None
        first = None  # (status, qs) for rooted[0] — the loop always queries it
                      # first; the no-serving fallback reuses that result
                      # instead of re-issuing the request (F-9).
        for i, s in enumerate(rooted):
            status, qs, err = http("GET",
                                   f"{BACKEND}/api/v1/questions?rootId={s['knowledgeNodeId']}",
                                   headers=auth)
            if i == 0:
                first = (status, qs)
            if status == 200 and isinstance(qs, list) and qs:
                serving = (s, qs)
                break
        if serving:
            s, qs = serving
            root = s["knowledgeNodeId"]
            record("learner-practice", True,
                   f"practice list → 200, {len(qs)} questions ({s.get('code')})",
                   "")
        else:
            root = rooted[0]["knowledgeNodeId"]
            status, qs = first
            record("learner-practice", status == 200 and isinstance(qs, list),
                   f"practice list → {status}, "
                   f"{len(qs) if isinstance(qs, list) else qs} questions",
                   "practice listing broken")
        status, rec, err = http("GET",
                                f"{BACKEND}/api/v1/learners/me/recommendations?rootId={root}",
                                headers=auth, timeout=120)
        policy = rec.get("policy") if isinstance(rec, dict) else None
        record("learner-nba", status == 200 and policy == "nba-rules/v1.3",
               f"recommendations → {status}, policy {policy}",
               "recommendation engine failing or policy regressed")

        # sprint-2 §8: Smart Lesson v2 — the explainable topic-scoped action.
        # The probe learner is fresh, so the honest cold-start action on any
        # servable topic is INSUFFICIENT_COVERAGE (or TUTOR_ENGAGED/UNCOVERED
        # dynamics on other topics); the invariants checked: v2 policy,
        # an action with a reason code, an evidence trace, and a target node.
        topic_id = next((q.get("primaryTopicNodeId") for q in qs
                         if q.get("primaryTopicNodeId")), None)
        if topic_id:
            status, lesson, err = http(
                "GET",
                f"{BACKEND}/api/v1/learners/me/smart-lesson"
                f"?rootId={root}&topicNodeId={topic_id}",
                headers=auth, timeout=120)
            ok = (status == 200 and isinstance(lesson, dict)
                  and lesson.get("policy") == "smart-lesson/v2"
                  and isinstance(lesson.get("action"), dict)
                  and bool(lesson["action"].get("reasonCode"))
                  and bool(lesson["action"].get("targetNodeId"))
                  and isinstance(lesson.get("evidence"), list)
                  and len(lesson["evidence"]) > 0)
            record("smart-lesson", ok,
                   f"smart-lesson → {status}, policy {lesson.get('policy') if isinstance(lesson, dict) else None}, "
                   f"action {lesson.get('action', {}).get('reasonCode') if isinstance(lesson, dict) else None}",
                   "smart lesson failing, policy regressed, or the evidence trace missing")
        else:
            record("smart-lesson", True,
                   "no topic-mapped servable question on the serving subject — skipped")

    # deployed-backend freshness (session-57 defect fixes)
    status, _, _ = http("GET", f"{BACKEND}/api/v1/questions?rootId={UNKNOWN_ROOT}",
                        headers=auth)
    record("scoping", status == 404,
           f"unknown practice root → {status}",
           "deployed backend predates f7adea7 subject scoping (serves all subjects' "
           "questions) — Render deploy of main required" if status == 200 else "")
    status, _, _ = http("GET", f"{BACKEND}/api/v1/learners/me/recommendations", headers=auth)
    record("missing-param", status == 400,
           f"recommendations without rootId → {status}",
           "deployed backend predates 5991187 (500 on missing param) — Render deploy "
           "of main required" if status == 500 else "")

    # contamination: cross-subject leakage. Since the 2026-09-14 §10 topic
    # mapping, 4CH1 legitimately SERVES its validated topic-mapped questions
    # (33 at the time the premise changed), so "zero served" is no longer the
    # invariant. The pilot-blocking defect this check exists for is a
    # question appearing on TWO subject surfaces. Compare the 4CH1 list
    # against every other rooted subject's list on the same account.
    ch1 = next((s for s in rooted if s.get("code") == "4CH1"), None)
    if ch1 is None:
        record("contamination", True,
               "4CH1 not activated yet — no learner-facing subject to contaminate",
               warn=True)
    else:
        status, qs, _ = http("GET",
                             f"{BACKEND}/api/v1/questions?rootId={ch1['knowledgeNodeId']}",
                             headers=auth)
        if status != 200 or not isinstance(qs, list):
            record("contamination", False,
                   f"4CH1 practice list → {status}", "practice listing broken")
        else:
            served = {q.get("id") for q in qs}
            overlap = {}
            for s in (x for x in rooted if x.get("code") != "4CH1"):
                st2, qs2, _ = http("GET",
                                   f"{BACKEND}/api/v1/questions?rootId={s['knowledgeNodeId']}",
                                   headers=auth)
                if st2 == 200 and isinstance(qs2, list):
                    for qid in served & {q.get("id") for q in qs2}:
                        overlap[qid] = s.get("code")
            record("contamination", not overlap,
                   f"4CH1 practice list → 200, {len(served)} questions served; "
                   f"cross-subject overlap with {len(rooted) - 1} other rooted "
                   f"subject(s): {len(overlap)}",
                   "questions are leaking across subject surfaces — pilot blocker"
                   if overlap else "")

    # teacher surface (only when teacher credentials are provided)
    check_teacher(ch1)

    # tutor generation leg (P1 2026-09-14): the one live-loop failure invisible
    # from the outside — probe with the monitor's learner session
    check_tutor(auth)


def check_teacher(ch1: dict | None) -> None:
    if not (TEACHER_EMAIL and TEACHER_PASSWORD):
        record("teacher", True, "teacher credentials not configured yet — skipping "
               "(set PILOT_TEACHER_* secrets once the pilot tutor is provisioned, "
               "runbook §2)", warn=True)
        return
    status, body, err = http("POST", f"{BACKEND}/api/v1/auth/login", body={
        "email": TEACHER_EMAIL, "password": TEACHER_PASSWORD})
    if status != 200 or not isinstance(body, dict):
        record("teacher", False, f"teacher login {status} {err}",
               "pilot tutor account broken")
        return
    roles = (body.get("user") or {}).get("roles", [])
    auth = {"Authorization": f"Bearer {body['accessToken']}"}
    if ch1 is None:
        record("teacher", True, f"login ok (roles {roles}); 4CH1 not activated yet",
               warn=True)
        return
    root = ch1["knowledgeNodeId"]
    status, edges, err = http("GET",
                              f"{BACKEND}/api/v1/teacher/concept-graph/edges?rootId={root}",
                              headers=auth)
    n = len(edges.get("edges", [])) if isinstance(edges, dict) else -1
    record("teacher", status == 200 and n > 0,
           f"concept-graph read → {status}, {n} edges",
           "teacher KG read model empty or failing")

    # sprint-2 §2: the class-intelligence read model (policy class-analytics/v1;
    # unmeasured topics must carry null mastery — honesty invariant, read-only)
    status, overview, err = http("GET",
                                 f"{BACKEND}/api/v1/teacher/class/overview?rootId={root}",
                                 headers=auth)
    if status == 200 and isinstance(overview, dict):
        honest = all(
            (t.get("learnersMeasured") or 0) > 0
            or (t.get("meanMastery") is None and t.get("masteryBand") == "UNMEASURED")
            for t in overview.get("topics", []))
        record("class-analytics",
               overview.get("policy") == "class-analytics/v1" and honest,
               f"overview → {status}, enrolled={overview.get('enrolledLearners')}, "
               f"measured {overview.get('measuredTopics')}/{overview.get('totalTopics')} topics",
               "class read model failing or fabricating values on unmeasured topics")
    else:
        record("class-analytics", False, f"overview → {status} {err}",
               "class read model failing")

    # sprint-2 §6/§7: the marking-throughput lane + queue intelligence
    # (deterministic paper-grouped queue with the mark→next chain, honest
    # throughput counts, rank reasons on the v3 review queue — read-only)
    status, queue, err = http("GET",
                              f"{BACKEND}/api/v1/teacher/marking/queue-v2?state=PENDING",
                              headers=auth)
    lane_ok, lane_note = False, f"queue-v2 → {status} {err or ''}"
    if status == 200 and isinstance(queue, dict):
        items = queue.get("items") or []
        groups = queue.get("groups") or []
        chain_ok = all(
            (item.get("nextAnswerId") == (items[i + 1]["answer"]["answerId"]
                                          if i + 1 < len(items) else None))
            for i, item in enumerate(items))
        states_ok = all(i["answer"].get("markingState") == "PENDING" for i in items)
        s_tp, tp, _ = http("GET", f"{BACKEND}/api/v1/teacher/marking/throughput",
                           headers=auth)
        s_v3, v3, _ = http("GET", f"{BACKEND}/api/v1/teacher/content/review-queue-v3",
                           headers=auth)
        tp_ok = (s_tp == 200 and isinstance(tp, dict)
                 and all(k in (tp.get("answersByState") or {})
                         for k in ("PENDING", "SMART_MARKED", "HUMAN_MARKED", "OVERRIDDEN")))
        v3_ok = (s_v3 == 200 and isinstance(v3, dict)
                 and all(isinstance(p.get("rankReasons"), list)
                         for p in (v3.get("papers") or [])[:50]))
        lane_ok = chain_ok and states_ok and tp_ok and v3_ok
        lane_note = (f"queue-v2 → {status} ({len(items)} pending / {len(groups)} groups, "
                     f"chain {'OK' if chain_ok else 'BROKEN'}), throughput → {s_tp}, "
                     f"review-v3 → {s_v3} ({len(v3.get('papers') or []) if isinstance(v3, dict) else 0} papers)")
    record("marking-lane", lane_ok, lane_note,
           "marking throughput lane failing: ordering/chain/throughput/queue-v3 regression")

    # sprint-2 §10: class-weakness targeting options — the teacher-side entry
    # point of the intervention loop (read-only). Invariants: the policy id,
    # every weak option carries at least one KNOWN explicit reason (no
    # synthetic score field), and unmeasured topics are never claimed weak
    # without a misconception/blocking reason.
    status, weakness, err = http(
        "GET", f"{BACKEND}/api/v1/teacher/tests/weakness-options?rootId={root}",
        headers=auth)
    known_reasons = {"LOW_MEAN_MASTERY", "ACTIVE_MISCONCEPTION_PRESENT",
                     "BLOCKED_BY_WEAK_PREREQUISITE"}
    if status == 200 and isinstance(weakness, dict):
        weak = weakness.get("weakTopics") or []
        reasons_ok = all(
            isinstance(w.get("reasons"), list) and w["reasons"]
            and set(w["reasons"]) <= known_reasons
            for w in weak)
        no_score = "weaknessScore" not in json.dumps(weakness)
        record("weakness-targeting",
               weakness.get("policy") == "test-builder-weakness/v1"
               and reasons_ok and no_score,
               f"weakness-options → {status} ({len(weak)} weak, "
               f"{len(weakness.get('coverageGaps') or [])} coverage gaps)",
               "class-weakness targeting failing or exposing non-transparent reasons")
    else:
        record("weakness-targeting", False, f"weakness-options → {status} {err}",
               "class-weakness targeting endpoint failing")

    # activation idempotency re-verification — weekly/manual only
    if EVENT_NAME == "schedule" and EVENT_SCHEDULE not in ("", "33 9 * * 0"):
        return
    # recovery 2026-09-13: the idempotent re-activation re-validates the whole
    # 340-node/496-edge seed and takes 60–120 s on the 0.1-CPU Render free tier
    # (measured) — the 30 s default timed out and misreported a healthy
    # production backend as failing. 300 s matches the health check's
    # cold-start tolerance philosophy; the check itself is unchanged.
    status, summary, err = http("POST", f"{BACKEND}/api/v1/teacher/concept-graph/activate",
                                headers=auth, timeout=300)
    if status == 200 and isinstance(summary, dict):
        nc, ec = summary.get("nodesCreated"), summary.get("edgesCreated")
        record("teacher-activation", nc == 0 and ec == 0,
               f"re-activation idempotent (created {nc} nodes / {ec} edges)",
               "activation is no longer idempotent — seed drift")
    else:
        record("teacher-activation", False, f"activation → {status} {err or ''}",
               "graph activation endpoint failing")


# ── 10. structured assessment / κ status ──────────────────────────────────────
def check_tutor(auth: dict | None) -> None:
    """P1 (2026-09-14): the tutor is the one leg of the student loop whose
    failure mode is invisible from the outside — retrieval and grounding can
    be healthy while generation 503s on every provider (live finding: 503
    tutor_unavailable, 'all providers failed', Render logs + the ADMIN-gated
    LLM health endpoint both unobservable). Probe it with the monitor's own
    learner account so the 6-hourly report distinguishes:
      200 grounded answer          -> LLM chain healthy
      200 deterministic refusal    -> honest no-evidence path (no LLM call)
      503 tutor_unavailable        -> LLM chain down (keys/quota) — flag it
    """
    if auth is None:
        record("tutor", False, "skipped — monitor login unavailable",
               "fix the monitor account first", warn=True)
        return
    status, body, err = http("POST", f"{BACKEND}/api/v1/tutor/ask", body={
        "question": "What is an atom?"}, headers=auth, timeout=180)
    if status == 200:
        keys = sorted(body.keys()) if isinstance(body, dict) else []
        record("tutor", True, f"200, shape {keys}",
               "" if "answer" in keys or "citations" in keys or "refusal" in keys
               else "200 but unexpected shape — verify tutor DTO")
        return
    if status == 503:
        record("tutor", False, f"503 {err or (body or {}).get('message','')}",
               "LLM chain failing on Render — check provider keys/quota "
               "(SYLLABAI_GROQ/GEMINI/OPENROUTER keys) and Render logs; "
               "retrieval+grounding are healthy, generation is not")
        return
    record("tutor", False, f"{status} {err}",
           "tutor endpoint regression — inspect backend")


def check_kappa() -> None:
    # Until T-C04 ships validated structured-assessment content there is no κ
    # to compute. The monitor carries the field so the weekly report shows it.
    record("kappa", True, "N/A — no structured-assessment content in production yet "
                          "(T-C04 pending; re-check when Smart Mark content ships)",
           warn=True)


# ── Discord report ─────────────────────────────────────────────────────────────
def report(wake_seconds: float) -> int:
    failed = [r for r in RESULTS if not r["ok"]]
    warned = [r for r in RESULTS if r.get("warn")]
    kind = ("weekly ops" if EVENT_NAME == "schedule" and EVENT_SCHEDULE == "33 9 * * 0"
            else "scheduled" if EVENT_NAME == "schedule" else "manual")
    color = 0x57F287 if not failed else 0xED4245
    lines = []
    for r in RESULTS:
        icon = "✅" if r["ok"] and not r.get("warn") else ("⚠️" if r.get("warn") else "❌")
        lines.append(f"{icon} **{r['name']}** — {r['detail']}")
        if r["note"]:
            lines.append(f"   ↳ {r['note']}")
    if EVENT_NAME == "schedule" and EVENT_SCHEDULE == "33 9 * * 0":
        lines += [
            "",
            "**Weekly operator checklist**",
            "1. Evidence export: run `scripts/ops/export_evidence.sh` (core repo) "
            "with `NEON_DSN` — attempts/answers are the irreplaceable pilot data "
            "(Neon free tier retains 7 days PITR).",
            "2. κ status: see the kappa line above.",
            "3. Triage pilot feedback + the defect log.",
        ]
    title = (f"SyllabAI Pilot Monitor — {kind}: "
             f"{'ALL GREEN' if not failed else f'{len(failed)} FAILING'}"
             + (f" ({len(warned)} advisory)" if warned and not failed else ""))
    embed = {"title": title, "description": "\n".join(lines)[:3900], "color": color,
             "footer": {"text": f"backend wake {wake_seconds:.0f}s · {BACKEND}"}}
    if RUN_URL:
        embed["url"] = RUN_URL
    if WEBHOOK:
        payload = {"username": "SyllabAI Pilot Monitor", "embeds": [embed]}
        try:
            http("POST", WEBHOOK, body=payload, timeout=15)
            print("[OK ] discord report posted")
        except Exception as e:  # noqa: BLE001
            print(f"::notice::discord report failed (non-fatal): {e}")
    print(f"\nSUMMARY: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks green "
          f"({len(failed)} failing, {len(warned)} advisory)")
    return 1 if failed else 0


def main() -> int:
    wake = check_health()
    check_cors()
    check_auth_401()
    check_teacher_guard()
    check_web_bundle()
    check_learner()
    check_kappa()

    return report(wake)


if __name__ == "__main__":
    sys.exit(main())
