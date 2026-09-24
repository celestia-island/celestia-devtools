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
import re
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
# Repos whose spills run in a CNB dev-quota workspace instead of the ci-farm build lane: the
# dev pool carries 1600 free core-hours/month against the build pool's 160, and build-pool burn
# is the binding constraint. It started with hikari (the single biggest build-lane consumer: 23
# of the 33 build dispatches in the 4.8 h after the #132/#133/#134 deploy) and now carries every
# repo whose task is a cargo check — celestia-devtools (python-check) and evernight-appliance
# (webui-check) still need non-cargo lane variants.
#
# Admission condition: the repo's *host* repository (`ci-infra-<repo>`, CNB-side only) must carry
# the lane pipeline on its master. `publish_lane_ref()` checks that at dispatch time and anything
# missing falls back to the build lane; the target repo itself no longer needs a `.cnb.yml` — the
# lane stopped reading the target's tree when it moved to the host.
DEV_QUOTA = {"shittim-chest", "evernight", "arona", "hikari",
             "plana", "entelecheia", "malkuth", "kirino"}
# Each dev-quota repo is validated by a CNB-side lane host (`ci-infra-<repo>`, a repo that
# exists only on cnb.cool) rather than by a pipeline file in the GitHub repo: the dispatcher
# pushes `spill/<sha10>` into the host, CNB runs the host's own `.cnb.yml`, and that pipeline
# clones the target's spilled merge from the org mirror. CNB allows one workspace per
# repository, so one host per repo also keeps the four lanes concurrent — consolidating them
# into a single host would serialise the whole dev pool.
LANE_HOST = os.environ.get("DISPATCH_LANE_HOST", "ci-infra-{repo}")
WS_CHECK_STAGE = "cargo-check"


def _env_int(name, default, low, high):
    """Bounded integer knob from the service env, falling back to the default.

    A typo in the env file must not take the daemon down at import time — systemd
    Restart=always would turn `DISPATCH_WS_ATTEMPTS=abc` into a crash loop that stops
    the whole overflow layer — and a zero/negative value must not silently disable the
    dev-quota lane (0 attempts raises without ever calling workspace/start).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        print(f"WARN: {name}={raw!r} is not an integer — using {default}", file=sys.stderr)
        return default
    if not low <= val <= high:
        print(f"WARN: {name}={val} outside [{low}, {high}] — using {default}", file=sys.stderr)
        return default
    return val


# Bounded retry budget for workspace/start: CNB answers 200 without `sn` while the
# dev-quota concurrency cap is fully occupied (transient — slots free up as running
# checks settle). Two attempts 30 s apart still missed twice on 2026-09-23
# (22:02/22:10) and each miss costs a build-lane dispatch, so the budget is three
# attempts 45 s apart (ws_start alone blocks up to 3 x 30 s HTTP + 90 s of sleep = 180 s;
# a whole spill adds the mirror budget — fetch 300 + merge 120 + ls-remote 60 + push 300 —
# and the build-lane call, so budget ~990 s per spill and ~2970 s per loop at the default
# batch of 3). That is why the per-loop spill batch is capped below: leftovers wait for the
# next poll instead of delaying resolve(), whose cancel-green step is what frees farm slots.
WS_START_ATTEMPTS = _env_int("DISPATCH_WS_ATTEMPTS", 3, 1, 10)
WS_START_RETRY_SEC = _env_int("DISPATCH_WS_RETRY_SEC", 45, 0, 300)
# Spills started per loop iteration (see the blocking note above).
SPILL_BATCH_PER_LOOP = _env_int("DISPATCH_SPILL_BATCH", 3, 1, 50)
# How long a dev-quota workspace may run without ever showing a terminal
# `WS_CHECK_STAGE` before the daemon assumes the ref carries no such stage and
# releases it. A repo whose master lacks `.cnb.yml` would otherwise hold a dev-quota
# slot until SPILL_TTL_SEC and starve every other dev-quota repo into build-lane
# fallbacks — the exact opposite of what this lane is for.
WS_STAGE_GRACE_SEC = _env_int("DISPATCH_WS_STAGE_GRACE_SEC", 900, 60, 3600)
GITCACHE = os.environ.get("DISPATCH_GITCACHE", "/var/lib/ci-dispatcher/git")
GIT_PROXY = os.environ.get("DISPATCH_GIT_PROXY", "http://daemon.node.local:7890")
EVENTS = {"cargo-check": "api_trigger_ci", "python-check": "api_trigger_py", "webui-check": "api_trigger_web"}
CARGO_ARGS = {"shittim-chest": "--exclude shittim_chest_tauri --exclude shittim_chest_tauri_mobile"}
APT_PACKAGES = {"shittim-chest": "libgtk-3-dev pkg-config libssl-dev",
                "plana": "libgtk-3-dev libsoup-3.0-dev libjavascriptcoregtk-4.1-dev libwebkit2gtk-4.1-dev pkg-config libssl-dev"}
ORG = "celestia-island"
THRESHOLD = _env_int("DISPATCH_THRESHOLD", 6, 0, 100)
POLL_SEC = _env_int("DISPATCH_POLL_SEC", 60, 5, 3600)
SPILL_TTL_SEC = _env_int("DISPATCH_SPILL_TTL_SEC", 2700, 60, 86400)

if WS_STAGE_GRACE_SEC >= SPILL_TTL_SEC - POLL_SEC:
    _fixed = max(60, min(WS_STAGE_GRACE_SEC, (SPILL_TTL_SEC - POLL_SEC) // 2))
    print(f"WARN: DISPATCH_WS_STAGE_GRACE_SEC={WS_STAGE_GRACE_SEC} leaves no room inside "
          f"DISPATCH_SPILL_TTL_SEC={SPILL_TTL_SEC} — using {_fixed}", file=sys.stderr)
    WS_STAGE_GRACE_SEC = _fixed
STATE_PATH = os.environ.get("DISPATCH_STATE", "/var/lib/ci-dispatcher/state.json")
GH = os.environ.get("GH_TOKEN", "")
FETCH = os.environ.get("GH_FETCH", "")
CNB = os.environ.get("CNB_TOKEN", "")
CNB_WS = os.environ.get("CNB_WS_TOKEN", "")


def log(msg):
    # choke point: even if a call site misses redact(), or logging itself fails
    # inside an except block (Python would print the raw __context__ chain),
    # nothing reaches journald unredacted. redact() is idempotent.
    print(time.strftime("[%FT%TZ] ") + redact(msg), flush=True)


# subprocess raises CalledProcessError whose str() embeds the full argv, and the
# fetch/push URLs carry the tokens inline (https://user:<PAT>@host/...). Those
# errors land in journald via the exception log lines below, so every message
# that may contain an exception goes through redact() first.
URL_CRED_RE = re.compile(r"(https?://[^/\s:@]+:)([^@\s/]+)@")


def redact(text):
    # bare-value pass first: tokens containing "@" would otherwise be split by
    # the URL pass and only partially masked
    for secret in (GH, FETCH, CNB, CNB_WS):
        if secret:
            text = text.replace(secret, "***")
    return URL_CRED_RE.sub(r"\1***@", text)


def redact_obj(obj):
    """Redact every string inside a decoded JSON value before it is re-serialized.

    `redact(json.dumps(r))` is not enough: a secret containing `"` or `\\` is escaped
    by json.dumps, so the bare-value pass no longer matches it and the escaped form
    reaches the log intact. Redacting the values first closes that hole.
    """
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(k): redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj


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
            log(f"census {repo}: {redact(str(e))}")
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


RECENT_TTL_SEC = _env_int("DISPATCH_RECENT_TTL_SEC", 2700, 60, 86400)


def load_state():
    try:
        st = json.load(open(STATE_PATH))
    except Exception:
        return {"_recent": {}}
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


# commit-tree refuses to run without an author identity (exit 128, "Author
# identity unknown") and the service env / gitcache bare repos carry none, which
# made every ws-spill fall back to the build lane. Pass a fixed synthetic
# identity explicitly so the spill merge commit never depends on ambient config.
SPILL_COMMIT_IDENTITY = (
    "-c", "user.name=ci-dispatcher",
    "-c", "user.email=ci-dispatcher@users.noreply.github.com",
)


def spill_merge_commit(d, tree_sha, parent_a, parent_b):
    import subprocess
    merge = subprocess.run(["git", "-C", d, *SPILL_COMMIT_IDENTITY, "commit-tree", tree_sha,
                            "-p", parent_a, "-p", parent_b],
                           input="spill merge" + chr(10), text=True, capture_output=True,
                           check=True, timeout=60)
    return merge.stdout.strip()


def spill_merge_tree(d, ours, theirs):
    """merge-tree the spilled sha onto master; raise (with conflict files) on conflict."""
    import subprocess
    tree = subprocess.run(["git", "-C", d, "merge-tree", "--write-tree", ours, theirs],
                          capture_output=True, text=True, timeout=120)
    tree_sha = tree.stdout.split(chr(10), 1)[0].strip()
    if tree.returncode != 0 or not tree_sha:
        # git puts conflict detail on stdout (after the tree line), not stderr
        conflicts = [ln.strip() for ln in tree.stdout.splitlines()[1:] if "CONFLICT" in ln]
        detail = conflicts or [ln.strip() for ln in (tree.stderr or "").splitlines() if ln.strip()]
        msg = f"merge-tree conflict for {theirs[:10]}"
        if detail:
            msg += ": " + "; ".join(detail[:3])[:300]
        raise RuntimeError(msg)
    return tree_sha


def ensure_sha_on_mirror(repo, sha):
    import subprocess
    d = f"{GITCACHE}/{repo}.git"
    os.makedirs(GITCACHE, exist_ok=True)
    if not os.path.isdir(d):
        subprocess.run(["git", "init", "-q", "--bare", d], check=True)
    env_gh = dict(os.environ, HTTPS_PROXY=GIT_PROXY, NO_PROXY="127.0.0.1,localhost,192.168.0.0/16")
    subprocess.run(["git", "-C", d, "fetch", "-q", f"https://langyo:{FETCH}@github.com/{ORG}/{repo}.git",
                    "+refs/heads/master:refs/heads/master", sha], check=True, env=env_gh, timeout=300)
    tree_sha = spill_merge_tree(d, "master", sha)
    msha = spill_merge_commit(d, tree_sha, "master", sha)
    env_cnb = {k: v for k, v in os.environ.items() if k.upper() not in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}
    cnb_url = f"https://cnb:{CNB}@cnb.cool/{ORG}/{repo}.git"
    spill_ref = f"refs/heads/spill/{sha[:10]}"
    # spill/<sha> refs are one-shot synthetic merge commits owned by this daemon;
    # a stale ref from a previous spill of the same sha makes a plain push fail
    # with non-fast-forward and doom the ws lane to its build-lane fallback.
    # A bare --force-with-lease is a no-op here: the gitcache bare repo has no
    # remote-tracking refs to lease against, so git rejects the push with
    # "stale info" exactly like a plain push. Pin the lease explicitly instead:
    # ls-remote the current value and require the push to land on exactly that
    # value (empty expect = ref must not exist yet). Never bare --force.
    lsr = subprocess.run(["git", "-C", d, "ls-remote", cnb_url, spill_ref],
                         capture_output=True, text=True, check=True, env=env_cnb, timeout=60)
    remote_val = lsr.stdout.split()[0] if lsr.stdout.strip() else ""
    subprocess.run(["git", "-C", d, "push", "-q", f"--force-with-lease={spill_ref}:{remote_val}",
                    cnb_url, f"{msha}:{spill_ref}"], check=True, env=env_cnb, timeout=300)
    return msha


def publish_lane_ref(repo, sha):
    """Publish `spill/<sha10>` into the repo's CNB-side lane host.

    Returns (branch, host_has_pipeline). The branch points at the *host's* own master, so the
    tree CNB checks out is the host's and the host's `.cnb.yml` is what runs; the pipeline then
    clones the target's spilled merge (which `ensure_sha_on_mirror` keeps on the org mirror
    under the same ref name). Fails closed: any git error propagates so `spill()` falls back to
    the build lane instead of starting a workspace that can never reach the gated stage.
    """
    import subprocess
    host = LANE_HOST.format(repo=repo)
    d = f"{GITCACHE}/{host}.git"
    os.makedirs(GITCACHE, exist_ok=True)
    if not os.path.isdir(d):
        subprocess.run(["git", "init", "-q", "--bare", d], check=True)
    # cnb.cool must be reached directly (the proxy throttles it to ~13 KB/s)
    env_cnb = {k: v for k, v in os.environ.items()
               if k.upper() not in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}
    cnb_url = f"https://cnb:{CNB}@cnb.cool/{ORG}/{host}.git"
    subprocess.run(["git", "-C", d, "fetch", "-q", "--force", cnb_url,
                    "+refs/heads/master:refs/heads/master"], check=True, env=env_cnb, timeout=300)
    has_pipeline = subprocess.run(["git", "-C", d, "cat-file", "-e", "master:.cnb.yml"],
                                  capture_output=True).returncode == 0
    ref = f"refs/heads/spill/{sha[:10]}"
    if not has_pipeline:
        # Do not leave a stray branch behind on a host that cannot run the lane.
        log(f"lane host {host} carries no .cnb.yml — not publishing {ref}")
        return f"spill/{sha[:10]}", False
    # Same one-shot-ref caution as the mirror push: a stale ref from a previous spill of the
    # same sha makes a plain push non-fast-forward, and a bare --force-with-lease is a no-op
    # without tracking refs, so pin the expected value explicitly.
    lsr = subprocess.run(["git", "-C", d, "ls-remote", cnb_url, ref],
                         capture_output=True, text=True, check=True, env=env_cnb, timeout=60)
    remote_val = lsr.stdout.split()[0] if lsr.stdout.strip() else ""
    subprocess.run(["git", "-C", d, "push", "-q", f"--force-with-lease={ref}:{remote_val}",
                    cnb_url, f"master:{ref}"], check=True, env=env_cnb, timeout=300)
    log(f"lane ref {host} spill/{sha[:10]} pipeline={'yes' if has_pipeline else 'NO'}")
    return f"spill/{sha[:10]}", has_pipeline


def ws_stop(sn):
    req = urllib.request.Request("https://api.cnb.cool/workspace/stop",
        data=json.dumps({"pipelineId": f"{sn}-001"}).encode(),
        headers={"Authorization": "Bearer " + CNB_WS, "Content-Type": "application/json",
                 "Accept": "application/vnd.cnb.api+json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def ws_start(repo, ref, attempts=None, delay=None):
    """Start a dev-quota workspace, retrying while the response carries no `sn`.

    CNB's workspace/start intermittently returns 200 without `sn` when the
    dev-quota concurrency cap is already occupied; that is transient (slots free
    up when running checks settle and stop), so spend the bounded retry budget
    before giving up and falling back to the build lane — every fallback spends
    the 160-core-hour build pool instead of the 1600-core-hour dev pool.
    Defaults come from DISPATCH_WS_ATTEMPTS / DISPATCH_WS_RETRY_SEC so the budget
    can be tuned in the service env without a redeploy.
    """
    attempts = WS_START_ATTEMPTS if attempts is None else attempts
    delay = WS_START_RETRY_SEC if delay is None else delay
    last = RuntimeError("workspace/start failed: no attempts made")
    for i in range(attempts):
        if i:
            time.sleep(delay)
        # Only `branch` is accepted here: passing a symbolic `ref` alongside `branch: master`
        # made CNB check out master instead (measured), and `branch` is required by the API.
        r = cnb_api(f"/{ORG}/{repo}/-/workspace/start", {"branch": ref})
        if r.get("sn"):
            return r
        last = RuntimeError(f"workspace/start returned no sn (attempt {i + 1}/{attempts}): "
                            f"{redact(json.dumps(redact_obj(r)))[:200]}")
    raise last


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
        # CNB allows one workspace per repository, so a second spill for a repo whose workspace
        # is still running cannot succeed: it would burn the whole retry budget and then fall
        # back to the build lane — spending exactly the pool this lane exists to save. Defer it
        # to the next poll instead (no record, so the census re-offers it).
        if any(t.get("mode") == "ws" and t.get("repo") == repo
               for k, t in state.items() if k != "_recent"):
            log(f"ws-spill {repo} run {entry['run_id']} deferred — {repo} already holds a workspace")
            return False
        try:
            ensure_sha_on_mirror(repo, sha)
            branch, has_pipeline = publish_lane_ref(repo, sha)
            if not has_pipeline:
                raise RuntimeError(f"{LANE_HOST.format(repo=repo)}: no .cnb.yml on master")
            host = LANE_HOST.format(repo=repo)
            r = ws_start(host, branch)
            # `build/status/{sn}` is scoped to the repo the pipeline belongs to, and since the
            # lane moved to the host that is NOT the target repo any more: polling the target
            # returned "Failed to retrieve pipeline", so `cs` stayed None and the run was
            # released at the grace window without ever posting a status or cancelling it.
            state[str(entry["run_id"])] = {"mode": "ws", "host": host, "sn": r["sn"], "repo": repo, "sha": sha,
                                           "url": redact(r.get("buildLogUrl", "")), "since": time.time()}
            log(f"ws-spill {repo} run {entry['run_id']} sha {sha[:10]} -> workspace {r['sn']}")
            return
        except Exception as e:
            log(f"ws-spill {repo} failed ({redact(str(e))}); falling back to build lane")
    r = cnb_api(f"/{ORG}/ci-farm/-/build/start",
                {"event": EVENTS.get(task, "api_trigger_ci"), "branch": "master",
                 "title": f"daemon spill {repo} {sha[:10]}", "env": env})
    state[str(entry["run_id"])] = {"mode": "build", "sn": r["sn"], "repo": repo, "sha": sha,
                                   "url": redact(r.get("buildLogUrl", "")), "since": time.time()}
    log(f"spill {repo} run {entry['run_id']} sha {sha[:10]} -> cnb {r['sn']}")


def post_status(repo, sha, state_, url):
    try:
        gh_api(f"/repos/{ORG}/{repo}/statuses/{sha}",
               {"state": state_, "context": "cnb/cargo-check", "target_url": url,
                "description": "dispatcher spill result"})
        log(f"status {repo} {sha[:10]} -> {state_}")
    except Exception as e:
        log(f"status post failed {repo}: {redact(str(e))}")


def resolve(state):
    for run_id, t in list(state.items()):
        if run_id == "_recent":
            continue  # bookkeeping dict, not a spill record
        repo, sha, sn, url = t["repo"], t["sha"], t["sn"], t.get("url", "")
        if time.time() - t["since"] > SPILL_TTL_SEC:
            # Same class as the grace release below: dropping the record without stopping
            # the workspace abandons a dev-quota slot, and without remember() the run is
            # re-spilled on the next poll (three workspace/start calls for one run, R3 T5).
            log(f"spill ttl exceeded {repo} run {run_id}; untracking (farm run untouched)")
            if t.get("mode") == "ws":
                try:
                    ws_stop(sn)
                    log(f"ws {sn} stopped after ttl expiry")
                except Exception as e:
                    log(f"ws stop {sn} failed: {redact(str(e))}")
            remember(state, sha)
            del state[run_id]
            continue
        try:
            if t.get("mode") == "ws":
                # records written before the lane moved to the host carry no "host" key
                d = cnb_api(f"/{ORG}/{t.get('host') or t['repo']}/-/build/status/{sn}")
                pipelines = list(d.get("pipelinesStatus", {}).values())
                stages = pipelines[0].get("stages", []) if pipelines else []
                cs = next((s for s in stages if s.get("name") == WS_CHECK_STAGE), None)
                st = None
                if cs and cs.get("status") in ("success", "error", "cancel"):
                    st = "success" if cs["status"] == "success" else "error"
                    try:
                        ws_stop(sn)
                        log(f"ws {sn} stopped after check {cs['status']}")
                    except Exception as e:
                        log(f"ws stop {sn} failed: {redact(str(e))}")
                elif cs is None and time.time() - t["since"] > WS_STAGE_GRACE_SEC:
                    # The stage is *absent*, so the spilled ref carries no dev-quota
                    # pipeline at all (e.g. the repo's master has no .cnb.yml yet): this
                    # would never reach a terminal state and the workspace would hold a
                    # dev-quota slot until SPILL_TTL_SEC, starving the other dev-quota
                    # repos into build-lane fallbacks. Fail closed: release the slot,
                    # post no status, leave the farm run. A stage that merely exists and
                    # is still running is healthy and must never be cut short here.
                    log(f"ws {sn} ({repo}) has no {WS_CHECK_STAGE} stage after "
                        f"{WS_STAGE_GRACE_SEC}s — stopping workspace, farm run untouched")
                    try:
                        ws_stop(sn)
                    except Exception as e:
                        # Keep tracking: the slot is still held, and dropping the record
                        # here would abandon it with nothing left to retry (R3 T4).
                        log(f"ws stop {sn} failed: {redact(str(e))}; keeping the record")
                        continue
                    # Without this the record is dropped and the same run is re-spilled
                    # on the very next poll (verified: a 15-minute churn loop that also
                    # ate the per-loop batch).
                    remember(state, sha)
                    del state[run_id]
                    continue
            else:
                st = cnb_api(f"/{ORG}/ci-farm/-/build/status/{sn}").get("status")
        except Exception as e:
            log(f"poll {sn}: {redact(str(e))}")
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
                log(f"cancel check {run_id}: {redact(str(e))}")
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
    log(f"ci-dispatcherd start: watched={len(WATCHED)} threshold={THRESHOLD} poll={POLL_SEC}s")
    while True:
        try:
            state = load_state()
            q = census()
            # state also carries the `_recent` bookkeeping dict — skip it or
            # the comprehension raises KeyError('sha') on every loop and the
            # daemon never spills (nor persists) anything again.
            spilled_shas = {t["sha"] for k, t in state.items() if k != "_recent"}
            excess = len(q) - THRESHOLD
            if excess > 0:
                now = time.time()
                recent = state.get("_recent", {})
                # Capped batch: resolve() — whose cancel-green step is what actually
                # frees farm slots — runs after this loop, and a spill whose
                # workspace/start misses the dev-quota cap blocks up to ~180 s, and a
                # whole spill ~990 s including the mirror budget. An unbounded batch (the
                # queue can hold ~200 runs) would delay those cancellations for hours and
                # spend the very build-pool core-hours this lane exists to save. The cap counts *started spills*, not scanned
                # entries: already-tracked runs at the head of the queue must not eat the
                # budget and starve everything behind them in a saturated queue.
                started = 0
                for entry in q[:excess]:
                    if started >= SPILL_BATCH_PER_LOOP:
                        break
                    if entry["sha"] in spilled_shas or str(entry["run_id"]) in state:
                        continue
                    ts = recent.get(entry["sha"])
                    if ts and now - ts < RECENT_TTL_SEC:
                        continue  # recently resolved on CNB; skip re-spill
                    if spill(entry, state) is False:
                        continue           # deferred: do not spend the batch budget
                    started += 1
                    spilled_shas.add(entry["sha"])
                    save_state(state)
            resolve(state)
            save_state(state)
            log(f"queued={len(q)} tracked={len(state) - 1} threshold={THRESHOLD}")
        except Exception as e:
            log(f"loop error: {redact(str(e))}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
