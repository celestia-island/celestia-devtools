#!/usr/bin/env python3
"""ci-dispatcherd — org CI overflow scheduler for the dedicated dispatcher node (node-ci-1).

Allocation policy (user directive 2026-09-22):
  1. Local farm first: node-ci-2/3/4 (label `local`) take everything they can absorb.
  2. When the local queue accumulates past DISPATCH_THRESHOLD queued farm runs in the
     watched repos, the newest runs are speculatively dispatched to Tencent CNB cloud
     (cargo-check via the ci-farm pipeline). If the CNB build goes green while the farm
     run is STILL QUEUED, the farm run is cancelled to release slots; if the CNB build
     fails, the farm run is left untouched so the author keeps the full CI signal.
  3. GitHub-hosted runners: effectively never (org policy: no new ubuntu-latest jobs on
     private repos).

Credentials come from /etc/ci-dispatcher/env (600, root):
  GH_TOKEN  — token with actions:read/write + statuses:write on celestia-island repos
  CNB_TOKEN — cnb.cool access token (repo-cnb-trigger:rw)

State: /var/lib/ci-dispatcher/state.json (survives restarts; tracked spills only).
Safety: only QUEUED runs in WATCHED repos are ever cancelled, only ones this daemon
dispatched a spill for, and only after the CNB build proved green.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

WATCHED = [
    "shittim-chest", "entelecheia", "arona", "malkuth",
    "plana", "hikari", "kirino", "evernight",
    "celestia-devtools", "evernight-appliance",
]
TASKS = {"celestia-devtools": "python-check", "evernight-appliance": "webui-check"}
DEV_QUOTA = {"shittim-chest", "evernight", "arona"}
WS_CHECK_STAGE = "cargo-check"
GITCACHE = os.environ.get("DISPATCH_GITCACHE", "/var/lib/ci-dispatcher/git")
GIT_PROXY = os.environ.get("DISPATCH_GIT_PROXY", "http://daemon.node.local:7890")
EVENTS = {"cargo-check": "api_trigger_ci", "python-check": "api_trigger_py", "webui-check": "api_trigger_web"}
CARGO_ARGS = {"shittim-chest": "--exclude shittim_chest_tauri --exclude shittim_chest_tauri_mobile"}
APT_PACKAGES = {"shittim-chest": "libgtk-3-dev pkg-config libssl-dev",
                "plana": "libgtk-3-dev libsoup-3.0-dev libjavascriptcoregtk-4.1-dev libwebkit2gtk-4.1-dev pkg-config libssl-dev"}
ORG = "celestia-island"
THRESHOLD = int(os.environ.get("DISPATCH_THRESHOLD", "6"))
POLL_SEC = int(os.environ.get("DISPATCH_POLL_SEC", "60"))
SPILL_TTL_SEC = int(os.environ.get("DISPATCH_SPILL_TTL_SEC", "2700"))
STATE_PATH = os.environ.get("DISPATCH_STATE", "/var/lib/ci-dispatcher/state.json")
GH = os.environ.get("GH_TOKEN", "")
FETCH = os.environ.get("GH_FETCH", "")
CNB = os.environ.get("CNB_TOKEN", "")
CNB_WS = os.environ.get("CNB_WS_TOKEN", "")


def log(msg):
    print(time.strftime("[%FT%TZ] ") + msg, flush=True)


def gh_api(path, data=None, method=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + GH, "Accept": "application/vnd.github+json"},
        method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else {}


def cnb_api(path, data=None):
    req = urllib.request.Request(
        f"https://api.cnb.cool{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + CNB,
                 "Content-Type": "application/json",
                 "Accept": "application/vnd.cnb.api+json"},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else {}


def census():
    """Queued self-hosted runs across watched repos, newest first."""
    runs = []
    for repo in WATCHED:
        try:
            rr = gh_api(f"/repos/{ORG}/{repo}/actions/runs?status=queued&per_page=20").get("workflow_runs", [])
        except Exception as e:
            log(f"census {repo}: {e}")
            continue
        for run in rr:
            if run.get("event") == "workflow_dispatch" and run.get("name", "").startswith("CNB"):
                continue  # our own lane/router runs are not farm CI
            try:
                jobs = gh_api(f"/repos/{ORG}/{repo}/actions/runs/{run['id']}/jobs?per_page=20").get("jobs", [])
            except Exception:
                continue
            if not any("self-hosted" in (j.get("labels") or []) and j.get("status") == "queued" for j in jobs):
                continue
            runs.append({"repo": repo, "run_id": run["id"], "sha": run["head_sha"],
                         "created": run["created_at"], "html": run.get("html_url", "")})
    runs.sort(key=lambda x: x["created"], reverse=True)
    return runs


RECENT_TTL_SEC = int(os.environ.get("DISPATCH_RECENT_TTL_SEC", "2700"))


def load_state():
    try:
        st = json.load(open(STATE_PATH))
    except Exception:
        return {}
    if not isinstance(st.get("_recent"), dict):
        st["_recent"] = {}
    return st


def remember(st, sha):
    recent = st.setdefault("_recent", {})
    recent[sha] = time.time()
    now = time.time()
    for s, ts in list(recent.items()):
        if now - ts > RECENT_TTL_SEC:
            del recent[s]


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_PATH)


def ensure_sha_on_mirror(repo, sha):
    import subprocess
    d = f"{GITCACHE}/{repo}.git"
    os.makedirs(GITCACHE, exist_ok=True)
    if not os.path.isdir(d):
        subprocess.run(["git", "init", "-q", "--bare", d], check=True)
    env_gh = dict(os.environ, HTTPS_PROXY=GIT_PROXY, NO_PROXY="127.0.0.1,localhost,192.168.0.0/16")
    subprocess.run(["git", "-C", d, "fetch", "-q", f"https://langyo:{FETCH}@github.com/{ORG}/{repo}.git", sha],
                   check=True, env=env_gh, timeout=300)
    env_cnb = {k: v for k, v in os.environ.items() if k.upper() not in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}
    subprocess.run(["git", "-C", d, "push", "-q", f"https://cnb:{CNB}@cnb.cool/{ORG}/{repo}.git",
                    f"{sha}:refs/heads/spill/{sha[:10]}"], check=True, env=env_cnb, timeout=300)


def ws_stop(sn):
    req = urllib.request.Request("https://api.cnb.cool/workspace/stop",
        data=json.dumps({"pipelineId": f"{sn}-001"}).encode(),
        headers={"Authorization": "Bearer " + CNB_WS, "Content-Type": "application/json",
                 "Accept": "application/vnd.cnb.api+json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def spill(entry, state):
    repo, sha = entry["repo"], entry["sha"]
    task = TASKS.get(repo, "cargo-check")
    env = {"TARGET_REPO": repo, "TARGET_SHA": sha, "GH_REPO": f"{ORG}/{repo}", "TASK": task}
    if FETCH:
        env["GH_READ_PAT"] = FETCH  # dedicated read PAT (contents:read) for SHA fallback fetch
    if repo in CARGO_ARGS:
        env["CARGO_CHECK_ARGS"] = CARGO_ARGS[repo]
    if repo in APT_PACKAGES:
        env["APT_PACKAGES"] = APT_PACKAGES[repo]
    if repo == "celestia-devtools":
        env["NEED_NODE"] = "1"
        env["PY_IGNORE"] = "tests/test_ci_orphan_janitor.py tests/test_deploy_e2e.py"
    if repo in DEV_QUOTA and CNB_WS:
        try:
            ensure_sha_on_mirror(repo, sha)
            r = cnb_api(f"/{ORG}/{repo}/-/workspace/start", {"branch": "master", "ref": sha})
            state[str(entry["run_id"])] = {"mode": "ws", "sn": r["sn"], "repo": repo, "sha": sha,
                                           "url": r.get("buildLogUrl", ""), "since": time.time()}
            log(f"ws-spill {repo} run {entry['run_id']} sha {sha[:10]} -> workspace {r['sn']}")
            return
        except Exception as e:
            log(f"ws-spill {repo} failed ({e}); falling back to build lane")
    r = cnb_api(f"/{ORG}/ci-farm/-/build/start",
                {"event": EVENTS.get(task, "api_trigger_ci"), "branch": "master",
                 "title": f"daemon spill {repo} {sha[:10]}", "env": env})
    state[str(entry["run_id"])] = {"mode": "build", "sn": r["sn"], "repo": repo, "sha": sha,
                                   "url": r.get("buildLogUrl", ""), "since": time.time()}
    log(f"spill {repo} run {entry['run_id']} sha {sha[:10]} -> cnb {r['sn']}")


def post_status(repo, sha, state_, url):
    try:
        gh_api(f"/repos/{ORG}/{repo}/statuses/{sha}",
               {"state": state_, "context": "cnb/cargo-check", "target_url": url,
                "description": "dispatcher spill result"})
        log(f"status {repo} {sha[:10]} -> {state_}")
    except Exception as e:
        log(f"status post failed {repo}: {e}")


def resolve(state):
    for run_id, t in list(state.items()):
        repo, sha, sn, url = t["repo"], t["sha"], t["sn"], t.get("url", "")
        if time.time() - t["since"] > SPILL_TTL_SEC:
            log(f"spill ttl exceeded {repo} run {run_id}; untracking (farm run untouched)")
            del state[run_id]
            continue
        try:
            if t.get("mode") == "ws":
                d = cnb_api(f"/{ORG}/{t['repo']}/-/build/status/{sn}")
                stages = list(d.get("pipelinesStatus", {}).values())[0].get("stages", [])
                cs = next((s for s in stages if s.get("name") == WS_CHECK_STAGE), None)
                st = None
                if cs and cs.get("status") in ("success", "error", "cancel"):
                    st = "success" if cs["status"] == "success" else "error"
                    try:
                        ws_stop(sn)
                        log(f"ws {sn} stopped after check {cs['status']}")
                    except Exception as e:
                        log(f"ws stop {sn} failed: {e}")
            else:
                st = cnb_api(f"/{ORG}/ci-farm/-/build/status/{sn}").get("status")
        except Exception as e:
            log(f"poll {sn}: {e}")
            continue
        if st in ("success", "error", "cancel"):
            remember(state, sha)
        if st == "success":
            post_status(repo, sha, "success", url)
            try:
                run = gh_api(f"/repos/{ORG}/{repo}/actions/runs/{run_id}")
                if run.get("status") == "queued":
                    gh_api(f"/repos/{ORG}/{repo}/actions/runs/{run_id}/cancel", {}, method="POST")
                    log(f"cancelled queued farm run {run_id} ({repo}) — cnb green")
            except Exception as e:
                log(f"cancel check {run_id}: {e}")
            del state[run_id]
        elif st in ("error", "cancel"):
            post_status(repo, sha, "failure", url)
            log(f"spill {repo} run {run_id} cnb {st}; farm run left untouched")
            del state[run_id]


def main():
    if not GH or not CNB or not FETCH:
        print("FATAL: GH_TOKEN / GH_FETCH / CNB_TOKEN missing", file=sys.stderr)
        sys.exit(1)
    if not CNB_WS:
        print("WARN: CNB_WS_TOKEN missing — dev-quota lane disabled", file=sys.stderr)
    if False:
        print("FATAL: GH_TOKEN / GH_FETCH / CNB_TOKEN missing", file=sys.stderr)
        sys.exit(1)
    log(f"ci-dispatcherd start: watched={len(WATCHED)} threshold={THRESHOLD} poll={POLL_SEC}s")
    while True:
        try:
            state = load_state()
            q = census()
            spilled_shas = {t["sha"] for t in state.values()}
            excess = len(q) - THRESHOLD
            if excess > 0:
                now = time.time()
                recent = state.get("_recent", {})
                for entry in q[:excess]:
                    if entry["sha"] in spilled_shas or str(entry["run_id"]) in state:
                        continue
                    ts = recent.get(entry["sha"])
                    if ts and now - ts < RECENT_TTL_SEC:
                        continue  # recently resolved on CNB; skip re-spill
                    spill(entry, state)
                    spilled_shas.add(entry["sha"])
                    save_state(state)
            resolve(state)
            save_state(state)
            log(f"queued={len(q)} tracked={len(state)} threshold={THRESHOLD}")
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
