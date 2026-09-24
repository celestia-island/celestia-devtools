"""Tests for the ci-dispatcherd spill merge commit identity.

The ws-spill path creates a synthetic merge commit in the gitcache bare repo.
That repo (and the service env) carries no git identity, so commit-tree must
have one passed explicitly — otherwise it exits 128 ("Author identity unknown")
and every dev-quota spill silently falls back to the build lane (#132 area).
"""

import importlib.util
import os
import subprocess
import sys

import pytest

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "ci_dispatcherd.py")
spec = importlib.util.spec_from_file_location("ci_dispatcherd", TOOL)
dsp = importlib.util.module_from_spec(spec)
sys.modules["ci_dispatcherd"] = dsp
spec.loader.exec_module(dsp)

FETCH_TOKEN = "github-pat-1111111111111111111111111"
CNB_TOKEN = "cnb-token-2222222222222222222222222"


def _called_process_error_with_cred_urls():
    """Mimic the real production failure argv shape (tokens inline in URLs)."""
    return subprocess.CalledProcessError(128, [
        "git", "-C", "/var/lib/ci-dispatcher/git/shittim-chest.git", "fetch", "-q",
        f"https://langyo:{FETCH_TOKEN}@github.com/celestia-island/shittim-chest.git",
        "+refs/heads/master:refs/heads/master", "a" * 40,
    ])


def test_redact_masks_url_credentials():
    out = dsp.redact(str(_called_process_error_with_cred_urls()))
    assert FETCH_TOKEN not in out
    assert "https://langyo:***@github.com/celestia-island/shittim-chest.git" in out
    assert "exit status 128" in out


def test_redact_masks_bare_secret_values(monkeypatch):
    monkeypatch.setattr(dsp, "GH", "gh-bearer-333")
    monkeypatch.setattr(dsp, "FETCH", FETCH_TOKEN)
    monkeypatch.setattr(dsp, "CNB", CNB_TOKEN)
    monkeypatch.setattr(dsp, "CNB_WS", "")
    out = dsp.redact(f"bearer gh-bearer-333 blew up near {FETCH_TOKEN} and {CNB_TOKEN}")
    assert "gh-bearer-333" not in out and FETCH_TOKEN not in out and CNB_TOKEN not in out
    assert out.count("***") == 3


def test_spill_failure_log_path_redacts_token(monkeypatch):
    """The ws-spill fallback log line must never carry the fetch/push tokens."""
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    err = _called_process_error_with_cred_urls()
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror",
                        lambda repo, sha: (_ for _ in ()).throw(err))
    seen = {}

    def fake_cnb_api(path, data=None):
        seen["path"] = path
        return {"sn": "sn-1"}

    monkeypatch.setattr(dsp, "cnb_api", fake_cnb_api)
    captured = []
    monkeypatch.setattr(dsp, "log", captured.append)
    state = {}
    dsp.spill({"repo": "shittim-chest", "run_id": 4242, "sha": "b" * 40}, state)
    assert state["4242"]["mode"] == "build"  # fell back to the build lane
    joined = "\n".join(captured)
    assert FETCH_TOKEN not in joined
    assert "langyo:***@github.com" in joined


@pytest.fixture
def no_git_identity(monkeypatch, tmp_path):
    """Strip every identity source: env vars, global/system config, HOME."""
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _bare_repo_with_two_parents(path):
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    tree = subprocess.run(["git", "-C", str(path), "mktree"], input="",
                          text=True, capture_output=True, check=True).stdout.strip()
    ident = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]
    parents = []
    for msg in ("parent-one", "parent-two"):
        sha = subprocess.run(["git", "-C", str(path), *ident, "commit-tree", tree],
                             input=msg + "\n", text=True, capture_output=True,
                             check=True).stdout.strip()
        parents.append(sha)
    return tree, parents[0], parents[1]


def test_spill_commit_identity_constant_is_declared():
    assert "user.name=ci-dispatcher" in dsp.SPILL_COMMIT_IDENTITY
    assert any(v.startswith("user.email=") for v in dsp.SPILL_COMMIT_IDENTITY)


def test_spill_merge_commit_works_without_ambient_identity(tmp_path, no_git_identity):
    repo = str(tmp_path / "fixture.git")
    tree, p1, p2 = _bare_repo_with_two_parents(repo)
    msha = dsp.spill_merge_commit(repo, tree, p1, p2)
    assert len(msha) == 40
    out = subprocess.run(
        ["git", "-C", repo, "show", "-s", "--format=%an%n%ae%n%P", msha],
        capture_output=True, text=True, check=True).stdout.strip().splitlines()
    assert out[0] == "ci-dispatcher"
    assert out[1].endswith("@users.noreply.github.com")
    # parent order must stay (master-side parent first, spilled sha second)
    assert out[2].split() == [p1, p2]


def _fixture_repo_with_conflicting_branches(path):
    """bare repo: master modifies f.txt; 'clean' adds g.txt; 'pr' conflicts on f.txt."""
    def git(*args, input=None, ident=False):
        env = dict(os.environ)
        if ident:
            env.update(GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.run(["git", "-C", str(path), *args], input=input, text=True,
                              capture_output=True, check=True, env=env).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    def blob(content):
        return git("hash-object", "-w", "--stdin", input=content)
    def tree(*entries):
        return git("mktree", input="".join(f"100644 blob {s}\t{n}\n" for s, n in entries))
    def commit(tree_sha, msg, parent=None):
        args = ["commit-tree", tree_sha] + (["-p", parent] if parent else [])
        return git(*args, input=msg + "\n", ident=True)
    f_base, f_master, f_pr, g_new = blob("one\n"), blob("two-master\n"), blob("two-pr\n"), blob("other\n")
    base = commit(tree((f_base, "f.txt")), "base")
    master = commit(tree((f_master, "f.txt")), "master", base)
    pr = commit(tree((f_pr, "f.txt")), "pr", base)
    clean = commit(tree((f_base, "f.txt"), (g_new, "g.txt")), "clean", base)
    git("update-ref", "refs/heads/master", master)
    return str(path), master, pr, clean


def test_spill_merge_tree_clean_merge(tmp_path, no_git_identity):
    repo, master, _pr, clean = _fixture_repo_with_conflicting_branches(tmp_path / "fx.git")
    tree_sha = dsp.spill_merge_tree(repo, master, clean)
    assert len(tree_sha) == 40


def test_spill_merge_tree_conflict_carries_detail(tmp_path, no_git_identity):
    repo, master, pr, _clean = _fixture_repo_with_conflicting_branches(tmp_path / "fx.git")
    with pytest.raises(RuntimeError) as ei:
        dsp.spill_merge_tree(repo, master, pr)
    assert "merge-tree conflict" in str(ei.value)
    assert "CONFLICT (content): Merge conflict in f.txt" in str(ei.value)


def test_redact_masks_token_containing_at(monkeypatch):
    """Bare-value pass must run before the URL pass or '@'-bearing tokens leak."""
    token = "abc@def"
    monkeypatch.setattr(dsp, "FETCH", token)
    out = dsp.redact(f"git fetch -q https://langyo:{token}@github.com/o/r.git")
    assert token not in out
    assert "https://langyo:***@github.com/o/r.git" in out


def test_redact_leaves_port_urls_alone():
    url = "see http://127.0.0.1:3080/notes@team for details"
    assert dsp.redact(url) == url


def test_all_exception_log_lines_are_redacted():
    """Guard every exception log site, not just the one covered behaviorally."""
    src = open(TOOL, encoding="utf-8").read().splitlines()
    through_redact = [ln for ln in src if "log(" in ln and "str(e)" in ln]
    # self-test the finder: the seven existing sites must be found
    assert len(through_redact) >= 7, f"finder pattern broke: matched {len(through_redact)}"
    for ln in through_redact:
        assert "redact(" in ln, f"str(e) logged without redact: {ln.strip()}"
    raw_e = [ln for ln in src if "log(" in ln and "{e}" in ln]
    assert raw_e == [], f"raw exception interpolation in log lines: {raw_e}"


def test_log_choke_point_redacts_everything(monkeypatch, capsys):
    """Even a call site that forgot redact() cannot leak through log()."""
    monkeypatch.setattr(dsp, "FETCH", FETCH_TOKEN)
    dsp.log(f"boom https://langyo:{FETCH_TOKEN}@github.com/o/r.git")
    out = capsys.readouterr().out
    assert FETCH_TOKEN not in out
    assert "langyo:***@github.com" in out


class _FakeCompleted:
    def __init__(self, stdout=""):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def test_spill_push_uses_force_with_lease(monkeypatch, tmp_path):
    """The spill ref push must carry an explicit --force-with-lease=<ref>:<expect>.

    The gitcache bare repo has no remote-tracking refs, so a BARE
    --force-with-lease is inert (git rejects a stale-ref overwrite with "stale
    info" just like a plain push) — the lease must be pinned to the value
    ls-remote reports. Mutation guard: dropping the flag, reverting to the bare
    form, or widening the refspec destination all turn this test red.
    """
    monkeypatch.setattr(dsp, "GITCACHE", str(tmp_path))
    monkeypatch.setattr(dsp, "FETCH", "fetch-token-x")
    monkeypatch.setattr(dsp, "CNB", CNB_TOKEN)
    sha = "b" * 40
    spill_ref = f"refs/heads/spill/{sha[:10]}"
    stale = "0" * 40
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "ls-remote" in argv:
            return _FakeCompleted(f"{stale}\t{spill_ref}\n")
        if "merge-tree" in argv:
            return _FakeCompleted("a" * 40 + "\n")
        if "commit-tree" in argv:
            return _FakeCompleted("c" * 40 + "\n")
        return _FakeCompleted("")

    monkeypatch.setattr(subprocess, "run", fake_run)
    msha = dsp.ensure_sha_on_mirror("shittim-chest", sha)
    assert msha == "c" * 40
    push_calls = [c for c in calls if "push" in c]
    assert len(push_calls) == 1, f"expected exactly one push, got {push_calls}"
    argv = push_calls[0]
    leases = [a for a in argv if a.startswith("--force-with-lease=")]
    assert leases == [f"--force-with-lease={spill_ref}:{stale}"], \
        f"explicit lease pinned to the stale value required, got {leases}"
    assert "--force" not in argv, "bare --force must never appear"
    refspecs = [a for a in argv if ":" in a and a.startswith("c" * 40)]
    assert refspecs == [f"{'c' * 40}:{spill_ref}"], \
        f"refspec destination must stay pinned to the spill ref, got {refspecs}"


def test_spill_push_lease_empty_expect_for_new_ref(monkeypatch, tmp_path):
    """When the spill ref does not exist remotely, the lease expects emptiness."""
    monkeypatch.setattr(dsp, "GITCACHE", str(tmp_path))
    monkeypatch.setattr(dsp, "FETCH", "fetch-token-x")
    monkeypatch.setattr(dsp, "CNB", CNB_TOKEN)
    sha = "b" * 40
    spill_ref = f"refs/heads/spill/{sha[:10]}"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "ls-remote" in argv:
            return _FakeCompleted("")  # ref absent on the remote
        if "merge-tree" in argv:
            return _FakeCompleted("a" * 40 + "\n")
        if "commit-tree" in argv:
            return _FakeCompleted("c" * 40 + "\n")
        return _FakeCompleted("")

    monkeypatch.setattr(subprocess, "run", fake_run)
    dsp.ensure_sha_on_mirror("shittim-chest", sha)
    push_argv = next(c for c in calls if "push" in c)
    assert f"--force-with-lease={spill_ref}:" in push_argv


def test_ws_start_retries_when_sn_missing(monkeypatch):
    """A `sn`-less start (dev-quota cap) is transient: retry, then succeed."""
    responses = [{}, {"sn": "sn-9", "buildLogUrl": "https://cnb.cool/x"}]
    seen = []
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: (seen.append(path), responses[len(seen) - 1])[1])
    sleeps = []
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: sleeps.append(s)),
                                                    "time": staticmethod(lambda: 0.0)}))
    r = dsp.ws_start("evernight", "c" * 40)
    assert r["sn"] == "sn-9"
    assert len(seen) == 2
    assert sleeps == [dsp.WS_START_RETRY_SEC]


def test_ws_start_gives_up_after_exhausted_retries(monkeypatch):
    """Every attempt missing `sn` must raise (caller falls back to build lane)."""
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {"error": "cap"})
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: 0.0)}))
    with pytest.raises(RuntimeError) as ei:
        dsp.ws_start("evernight", "c" * 40)
    assert f"no sn (attempt {dsp.WS_START_ATTEMPTS}/{dsp.WS_START_ATTEMPTS})" in str(ei.value)
    assert "cap" in str(ei.value)


def test_ws_start_default_budget_beats_measured_contention():
    """The default budget is evidence-backed, not arbitrary: two attempts 30 s apart
    still missed the dev-quota cap twice in production (2026-09-23 22:02 / 22:10),
    and every miss spends the 160-core-hour build pool instead of the dev pool.
    A silent rollback below that measured floor must fail here."""
    assert dsp.WS_START_ATTEMPTS >= 3
    assert dsp.WS_START_RETRY_SEC >= 45


def test_ws_start_budget_is_env_tunable(monkeypatch):
    """The retry budget must be readable from the service env, not hardcoded."""
    monkeypatch.setattr(dsp, "WS_START_ATTEMPTS", 4)
    monkeypatch.setattr(dsp, "WS_START_RETRY_SEC", 7)
    calls = []
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: (calls.append(path), {"error": "cap"})[1])
    sleeps = []
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: sleeps.append(s)),
                                                    "time": staticmethod(lambda: 0.0)}))
    with pytest.raises(RuntimeError):
        dsp.ws_start("hikari", "c" * 40)
    assert len(calls) == 4
    assert sleeps == [7, 7, 7]


def test_ws_start_failure_message_redacts_credentials(monkeypatch):
    """A token-bearing response body must not leak through the raised message."""
    monkeypatch.setattr(dsp, "FETCH", FETCH_TOKEN)
    body = {"error": f"denied https://langyo:{FETCH_TOKEN}@github.com/o/r.git"}
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: body)
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: 0.0)}))
    with pytest.raises(RuntimeError) as ei:
        dsp.ws_start("evernight", "c" * 40)
    assert FETCH_TOKEN not in str(ei.value)
    assert "langyo:***@github.com" in str(ei.value)


def test_ws_start_no_retry_on_first_success(monkeypatch):
    """A healthy first response must not pay the retry delay."""
    seen = []
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: (seen.append(path), {"sn": "sn-1"})[1])
    sleeps = []
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: sleeps.append(s)),
                                                    "time": staticmethod(lambda: 0.0)}))
    assert dsp.ws_start("evernight", "c" * 40)["sn"] == "sn-1"
    assert len(seen) == 1 and sleeps == []


def test_dev_quota_members_are_watched():
    """A dev-quota repo the census does not watch would never be spilled at all."""
    assert dsp.DEV_QUOTA <= set(dsp.WATCHED)


def test_dev_quota_repos_route_to_the_lane_host(monkeypatch):
    """hikari is validated by its CNB-side host; other WATCHED repos keep spilling to ci-farm."""
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror", lambda repo, sha: "e" * 40)
    monkeypatch.setattr(dsp, "publish_lane_ref", lambda repo, sha: (f"spill/{sha[:10]}", True))
    started = []
    monkeypatch.setattr(dsp, "ws_start",
                        lambda repo, ref: (started.append((repo, ref)), {"sn": "sn-ws"})[1])
    state = {}
    dsp.spill({"repo": "hikari", "run_id": 11, "sha": "b" * 40}, state)
    assert state["11"]["mode"] == "ws"
    assert state["11"]["host"] == "ci-infra-hikari"   # resolve() polls this, not the target repo
    assert started == [("ci-infra-hikari", "spill/" + "b" * 10)]

    paths = []
    monkeypatch.setattr(dsp, "cnb_api",
                        lambda path, data=None: (paths.append(path), {"sn": "sn-build"})[1])
    # celestia-devtools is still a build-lane repo (its lane variant is python, not cargo)
    dsp.spill({"repo": "celestia-devtools", "run_id": 12, "sha": "c" * 40}, state)
    assert state["12"]["mode"] == "build"
    assert paths and paths[0].endswith("/celestia-island/ci-farm/-/build/start")


def test_lane_host_without_a_pipeline_falls_back_to_the_build_lane(monkeypatch):
    """A host whose master carries no .cnb.yml cannot reach the gated stage, so the spill must
    go to the build lane instead of holding a workspace slot."""
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror", lambda repo, sha: "e" * 40)
    monkeypatch.setattr(dsp, "publish_lane_ref", lambda repo, sha: (f"spill/{sha[:10]}", False))
    started = []
    monkeypatch.setattr(dsp, "ws_start", lambda repo, ref: (started.append(ref), {"sn": "sn-ws"})[1])
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {"sn": "sn-build"})
    state = {}
    dsp.spill({"repo": "hikari", "run_id": 31, "sha": "b" * 40}, state)
    assert state["31"]["mode"] == "build"
    assert started == []


def test_same_repo_workspace_defers_instead_of_burning_the_budget(monkeypatch):
    """CNB allows one workspace per repository: a second same-repo spill must wait for the next
    poll rather than exhaust the retry budget and drop to the build lane."""
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    touched = []
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror", lambda repo, sha: touched.append("mirror") or "e" * 40)
    monkeypatch.setattr(dsp, "publish_lane_ref", lambda repo, sha: touched.append("lane") or ("spill/x", True))
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: touched.append("build") or {"sn": "s"})
    monkeypatch.setattr(dsp, "ws_start", lambda repo, ref: touched.append("ws") or {"sn": "sn"})
    state = {"9": {"mode": "ws", "sn": "sn-running", "repo": "hikari", "sha": "a" * 40,
                   "url": "", "since": 0.0}}
    dsp.spill({"repo": "hikari", "run_id": 41, "sha": "b" * 40}, state)
    assert touched == []          # neither a workspace nor a build-lane dispatch
    assert "41" not in state      # no record: the census re-offers it next poll
    dsp.spill({"repo": "arona", "run_id": 42, "sha": "c" * 40}, state)
    assert state["42"]["mode"] == "ws"


def test_ws_start_budget_reads_the_service_env():
    """The knobs must come from the service env (systemd EnvironmentFile), not be
    hardcoded — the previous assertion only compared module attributes to each other."""
    script = (
        "import importlib.util;"
        f"spec=importlib.util.spec_from_file_location('d', {os.path.abspath(TOOL)!r});"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "print(m.WS_START_ATTEMPTS, m.WS_START_RETRY_SEC, m.SPILL_BATCH_PER_LOOP, m.WS_STAGE_GRACE_SEC,"
        "m.THRESHOLD, m.POLL_SEC, m.SPILL_TTL_SEC)"
    )
    env = dict(os.environ, DISPATCH_WS_ATTEMPTS="2", DISPATCH_WS_RETRY_SEC="7",
               DISPATCH_SPILL_BATCH="5", DISPATCH_WS_STAGE_GRACE_SEC="120",
               DISPATCH_THRESHOLD="abc", DISPATCH_POLL_SEC="0",
               DISPATCH_SPILL_TTL_SEC="1500")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    # the malformed pre-existing knobs fall back instead of crashing the import
    # a valid non-default proves the knob is actually read, not a hardcoded 6/60
    assert out.stdout.split() == ["2", "7", "5", "120", "6", "60", "1500"]


def test_env_int_rejects_garbage_and_out_of_range(monkeypatch, capsys):
    """A bad env value must fall back to the default instead of crashing the daemon at
    import time (systemd Restart=always would turn a typo into a crash loop)."""
    monkeypatch.setenv("DSP_TEST_KNOB", "abc")
    assert dsp._env_int("DSP_TEST_KNOB", 3, 1, 10) == 3
    monkeypatch.setenv("DSP_TEST_KNOB", "0")
    assert dsp._env_int("DSP_TEST_KNOB", 3, 1, 10) == 3
    monkeypatch.setenv("DSP_TEST_KNOB", "99")
    assert dsp._env_int("DSP_TEST_KNOB", 3, 1, 10) == 3
    monkeypatch.setenv("DSP_TEST_KNOB", "4")
    assert dsp._env_int("DSP_TEST_KNOB", 3, 1, 10) == 4
    monkeypatch.delenv("DSP_TEST_KNOB")
    assert dsp._env_int("DSP_TEST_KNOB", 3, 1, 10) == 3
    assert capsys.readouterr().err.count("WARN") == 3


def _raise_ws_start(monkeypatch, body):
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: body)
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: 0.0)}))
    with pytest.raises(RuntimeError) as ei:
        dsp.ws_start("evernight", "c" * 40)
    return str(ei.value)


def test_ws_start_message_never_leaks_a_secret_through_escaping_or_truncation(monkeypatch):
    """Redaction happens on the decoded values/keys *before* json.dumps, so neither the
    dump's escaping nor the 200-byte cut can expose a secret. (An earlier version of this
    test asserted the truncate-after-redact order, which became unobservable once
    redact_obj moved redaction ahead of serialization — R2 review.)"""
    key_secret = 'KEY"SECRET' + "k" * 30
    monkeypatch.setattr(dsp, "FETCH", key_secret)
    msg = _raise_ws_start(monkeypatch, {key_secret: "v"})
    assert key_secret not in msg
    assert key_secret.replace('"', '\\"') not in msg

    val_secret = "VALSECRET" + "v" * 40
    monkeypatch.setattr(dsp, "FETCH", val_secret)
    head = len('workspace/start returned no sn (attempt 1/3): {"error": "')
    # land the secret at byte 180: a cut at 200 would keep 20 bytes of it
    msg = _raise_ws_start(monkeypatch, {"error": "y" * (180 - head) + " " + val_secret})
    assert val_secret not in msg and "VALSECRET" not in msg and len(msg) < 400


def test_ws_start_redacts_escaped_secret(monkeypatch):
    """json.dumps escapes quotes, so a bare-value pass over the dumped text misses a
    secret containing one; redacting the decoded values first keeps it out."""
    weird = 'ab"cd' + "9" * 20
    monkeypatch.setattr(dsp, "FETCH", weird)
    msg = _raise_ws_start(monkeypatch, {"error": f"denied {weird}"})
    assert weird not in msg
    assert weird.replace('"', '\\"') not in msg


def test_dev_quota_repo_uses_build_lane_without_ws_token(monkeypatch):
    """No workspace token means the dev-quota lane is off — it must not be attempted."""
    monkeypatch.setattr(dsp, "CNB_WS", "")
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    touched = []
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror",
                        lambda repo, sha: (touched.append(repo), "e" * 40)[1])
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {"sn": "sn-build"})
    state = {}
    dsp.spill({"repo": "hikari", "run_id": 21, "sha": "b" * 40}, state)
    assert state["21"]["mode"] == "build"
    assert touched == []


def _resolve_fixture(monkeypatch, now, since, stages):
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {
        "status": "pending", "pipelinesStatus": {"p1": {"stages": stages}}})
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: now)}))
    stopped, logs = [], []
    monkeypatch.setattr(dsp, "ws_stop", lambda sn: stopped.append(sn))
    monkeypatch.setattr(dsp, "log", logs.append)
    state = {"7": {"mode": "ws", "sn": "sn-ws", "repo": "hikari", "sha": "a" * 40,
                   "url": "", "since": since}}
    return state, stopped, logs


def test_resolve_releases_workspace_without_the_check_stage(monkeypatch):
    """`.cnb.yml` missing on master (or a renamed stage) would otherwise hold a
    dev-quota slot until SPILL_TTL_SEC and starve the other dev-quota repos."""
    now = 1_000_000.0
    state, stopped, logs = _resolve_fixture(monkeypatch, now, now - dsp.WS_STAGE_GRACE_SEC - 1,
                                            [{"name": "verify-ref", "status": "success"}])
    dsp.resolve(state)
    assert stopped == ["sn-ws"]
    assert "7" not in state
    assert any(f"no {dsp.WS_CHECK_STAGE} stage" in m for m in logs)


def test_resolve_leaves_workspace_without_stage_inside_the_grace_window(monkeypatch):
    now = 1_000_000.0
    state, stopped, logs = _resolve_fixture(monkeypatch, now, now - 30,
                                            [{"name": "verify-ref", "status": "success"}])
    dsp.resolve(state)
    assert stopped == []
    assert "7" in state and logs == []


def test_spill_batch_is_capped_per_loop(monkeypatch):
    """resolve() (cancel-green) runs after the spill loop; an unbounded batch would
    delay those cancellations behind up to ~180 s of ws retry each."""

    class _Stop(BaseException):
        pass

    entries = [{"repo": "hikari", "run_id": i, "sha": f"{i:040d}"} for i in range(1, 11)]
    monkeypatch.setattr(dsp, "GH", "g")
    monkeypatch.setattr(dsp, "CNB", "c")
    monkeypatch.setattr(dsp, "FETCH", "f")
    monkeypatch.setattr(dsp, "CNB_WS", "w")
    monkeypatch.setattr(dsp, "load_state", lambda: {"_recent": {}})
    monkeypatch.setattr(dsp, "census", lambda: entries)
    monkeypatch.setattr(dsp, "save_state", lambda st: None)
    monkeypatch.setattr(dsp, "resolve", lambda st: None)
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    spilled = []
    monkeypatch.setattr(dsp, "spill", lambda entry, state: spilled.append(entry["run_id"]))

    def boom(_):
        raise _Stop()

    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(boom),
                                                    "time": staticmethod(lambda: 0.0)}))
    with pytest.raises(_Stop):
        dsp.main()
    assert spilled == [1, 2, 3]
    assert dsp.SPILL_BATCH_PER_LOOP == 3


def test_resolve_survives_empty_pipeline_status(monkeypatch):
    """An empty pipelinesStatus must not blow up the poll (the old code indexed [0]
    and the IndexError was swallowed into a noisy `poll ...: list index out of range`)."""
    now = 1_000_000.0
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {"status": "pending", "pipelinesStatus": {}})
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: now)}))
    logs = []
    monkeypatch.setattr(dsp, "log", logs.append)
    state = {"7": {"mode": "ws", "sn": "sn-ws", "repo": "hikari", "sha": "a" * 40,
                   "url": "", "since": now - 5}}
    dsp.resolve(state)
    assert "7" in state
    assert logs == []  # the old [0] indexing logged "poll sn-ws: list index out of range"


def test_resolve_never_cuts_a_running_check_stage(monkeypatch):
    """The grace guard is for a *missing* stage, not for slowness: a check that is still
    running past the grace window is healthy and must be left alone (R2 found the old
    condition cut a 901 s `running` stage in half, without posting anything)."""
    now = 1_000_000.0
    state, stopped, logs = _resolve_fixture(monkeypatch, now, now - dsp.WS_STAGE_GRACE_SEC - 1,
                                            [{"name": dsp.WS_CHECK_STAGE, "status": "running"}])
    dsp.resolve(state)
    assert stopped == []
    assert "7" in state
    assert logs == []


def test_grace_release_remembers_the_sha(monkeypatch):
    """Releasing a workspace whose ref has no pipeline must not resurface the same run on
    the next poll (R2 measured a 15-minute churn loop that also ate the spill budget)."""
    now = 1_000_000.0
    state, _, _ = _resolve_fixture(monkeypatch, now, now - dsp.WS_STAGE_GRACE_SEC - 1,
                                   [{"name": "verify-ref", "status": "success"}])
    dsp.resolve(state)
    assert state["_recent"]["a" * 40] == now


def _loop_fixture(monkeypatch, entries, state, threshold=1):
    class _Stop(BaseException):
        pass

    monkeypatch.setattr(dsp, "THRESHOLD", threshold)

    monkeypatch.setattr(dsp, "GH", "g")
    monkeypatch.setattr(dsp, "CNB", "c")
    monkeypatch.setattr(dsp, "FETCH", "f")
    monkeypatch.setattr(dsp, "CNB_WS", "w")
    monkeypatch.setattr(dsp, "load_state", lambda: state)
    monkeypatch.setattr(dsp, "census", lambda: entries)
    monkeypatch.setattr(dsp, "save_state", lambda st: None)
    monkeypatch.setattr(dsp, "resolve", lambda st: None)
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    spilled = []
    monkeypatch.setattr(dsp, "spill", lambda entry, st: spilled.append(entry["run_id"]))

    def boom(_):
        raise _Stop()

    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(boom),
                                                    "time": staticmethod(lambda: 0.0)}))
    return spilled, _Stop


def test_tracked_entries_do_not_eat_the_spill_budget(monkeypatch):
    """The cap counts started spills: runs already tracked at the head of the queue must
    not starve the untracked ones behind them (R2's saturated-queue case: 6 queued, the
    first 3 tracked, and the old slice spilled nothing for three polls)."""
    entries = [{"repo": "hikari", "run_id": i, "sha": f"{i:040d}"} for i in range(1, 7)]
    state = {"_recent": {}}
    for e in entries[:3]:
        state[str(e["run_id"])] = {"mode": "build", "sn": "sn-x", "repo": "hikari",
                                   "sha": e["sha"], "url": "", "since": 0.0}
    spilled, stop = _loop_fixture(monkeypatch, entries, state, threshold=0)
    with pytest.raises(stop):
        dsp.main()
    assert spilled == [4, 5, 6]


def test_ws_start_explicit_budget_overrides_the_env_constants(monkeypatch):
    """preflight-ws.py calls ws_start(attempts=1, delay=0); that contract must not silently
    become the env budget (R3 H1)."""
    monkeypatch.setattr(dsp, "WS_START_ATTEMPTS", 5)
    monkeypatch.setattr(dsp, "WS_START_RETRY_SEC", 99)
    calls, sleeps = [], []
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: (calls.append(path), {"error": "cap"})[1])
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: sleeps.append(s)),
                                                    "time": staticmethod(lambda: 0.0)}))
    with pytest.raises(RuntimeError):
        dsp.ws_start("hikari", "c" * 40, attempts=1, delay=0)
    assert len(calls) == 1 and sleeps == []


def test_ttl_release_stops_the_workspace_and_remembers(monkeypatch):
    """The TTL branch carried the same defect class as the grace release: it dropped the
    record without stopping the workspace (leaking a dev-quota slot) and without remember()
    (R3 T5: three workspace/start calls for a single run)."""
    now = 1_000_000.0
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {
        "status": "pending",
        "pipelinesStatus": {"p1": {"stages": [{"name": dsp.WS_CHECK_STAGE, "status": "running"}]}}})
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: now)}))
    stopped, logs = [], []
    monkeypatch.setattr(dsp, "ws_stop", lambda sn: stopped.append(sn))
    monkeypatch.setattr(dsp, "log", logs.append)
    state = {"7": {"mode": "ws", "sn": "sn-ws", "repo": "hikari", "sha": "a" * 40,
                   "url": "", "since": now - dsp.SPILL_TTL_SEC - 1}}
    dsp.resolve(state)
    assert stopped == ["sn-ws"]
    assert "7" not in state
    assert state["_recent"]["a" * 40] == now


def test_grace_release_keeps_the_record_when_ws_stop_fails(monkeypatch):
    """A failed ws_stop means the slot is still held — dropping the record would abandon it
    with nothing left to retry (R3 T4)."""
    now = 1_000_000.0
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {
        "status": "pending",
        "pipelinesStatus": {"p1": {"stages": [{"name": "verify-ref", "status": "success"}]}}})
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: now)}))
    logs = []

    def boom(sn):
        raise RuntimeError("stop failed")

    monkeypatch.setattr(dsp, "ws_stop", boom)
    monkeypatch.setattr(dsp, "log", logs.append)
    state = {"7": {"mode": "ws", "sn": "sn-ws", "repo": "hikari", "sha": "a" * 40,
                   "url": "", "since": now - dsp.WS_STAGE_GRACE_SEC - 1}}
    dsp.resolve(state)
    assert "7" in state
    assert "_recent" not in state
    assert any("keeping the record" in m for m in logs)


def test_grace_window_is_clamped_inside_the_spill_ttl():
    """A grace window that cannot fit inside the TTL would make every ws record die on the
    TTL row instead (R3 T9: 60 s TTL + 3600 s grace was accepted)."""
    script = (
        "import importlib.util;"
        f"spec=importlib.util.spec_from_file_location('d', {os.path.abspath(TOOL)!r});"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "print(m.SPILL_TTL_SEC, m.WS_STAGE_GRACE_SEC, m.POLL_SEC)"
    )
    env = dict(os.environ, DISPATCH_SPILL_TTL_SEC="300", DISPATCH_WS_STAGE_GRACE_SEC="3600",
               DISPATCH_POLL_SEC="45")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    ttl, grace, poll = (int(x) for x in out.stdout.split())
    assert ttl == 300 and 60 <= grace < ttl - poll




def test_publish_lane_ref_pushes_the_spill_branch_into_the_host(monkeypatch, tmp_path):
    """The pushed refspec must be the one-shot `spill/<sha10>` branch of the lane host, pinned
    with an explicit force-with-lease (a bare lease is a no-op without tracking refs)."""
    calls = []

    class _Done:
        def __init__(self, out="", rc=0):
            self.stdout, self.returncode = out, rc

    def fake_run(argv, **kw):
        calls.append(argv)
        if "cat-file" in argv:
            return _Done(rc=0)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(dsp, "GITCACHE", str(tmp_path))
    sha = "d4e7ada83ca7fb50b79daf83446acee1814d12fb"
    branch, has_pipeline = dsp.publish_lane_ref("hikari", sha)
    assert branch == "spill/d4e7ada83c" and has_pipeline is True
    push = next(c for c in calls if "push" in c)
    assert "master:refs/heads/spill/d4e7ada83c" in push
    assert any(a.startswith("--force-with-lease=refs/heads/spill/d4e7ada83c:") for a in push)
    assert any("ci-infra-hikari.git" in a for a in push)


def test_publish_lane_ref_reports_a_host_without_the_pipeline(monkeypatch, tmp_path):
    class _P:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout = rc, out

    def fake_run(argv, **kw):
        if "cat-file" in argv:
            return _P(rc=1)
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(dsp, "GITCACHE", str(tmp_path))
    _, has_pipeline = dsp.publish_lane_ref("hikari", "a" * 40)
    assert has_pipeline is False


def test_ws_start_posts_the_branch_only_body(monkeypatch):
    """Measured against CNB: `branch` is required and passing `ref` alongside `branch: master`
    made the workspace check out master instead of the requested ref, so the body must carry the
    branch and nothing else."""
    seen = []
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: (seen.append((path, data)), {"sn": "sn-1"})[1])
    dsp.ws_start("ci-infra-hikari", "spill/abc1234567")
    assert seen == [("/celestia-island/ci-infra-hikari/-/workspace/start",
                     {"branch": "spill/abc1234567"})]


def test_publish_lane_ref_propagates_git_failures(monkeypatch, tmp_path):
    """Publishing must fail closed: an error has to reach spill() so the run goes to the build
    lane, never a workspace that cannot reach the gated stage."""
    def boom(argv, **kw):
        raise subprocess.CalledProcessError(128, argv)

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(dsp, "GITCACHE", str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        dsp.publish_lane_ref("hikari", "a" * 40)


class _P:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout = rc, out


def _lane_probe(monkeypatch, tmp_path, ls_out="", cat_rc=0, fail_on=None, calls=None):
    """Drive publish_lane_ref with a fake git, recording every argv."""
    calls = calls if calls is not None else []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if fail_on and fail_on in argv:
            raise subprocess.CalledProcessError(128, argv)
        if "cat-file" in argv:
            return _P(rc=cat_rc)
        if "ls-remote" in argv:
            return _P(out=ls_out)
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(dsp, "GITCACHE", str(tmp_path))
    return calls


def test_publish_lane_ref_pins_the_lease_value(monkeypatch, tmp_path):
    """A bare --force-with-lease is inert in this topology (#134), so the expected remote value
    must be the one ls-remote just reported — not an empty string."""
    calls = _lane_probe(monkeypatch, tmp_path, ls_out="f583087fcba3deadbeef\trefs/heads/spill/x\n")
    dsp.publish_lane_ref("hikari", "d4e7ada83ca7fb50b79daf83446acee1814d12fb")
    push = next(argv for argv, _ in calls if "push" in argv)
    assert any(a == "--force-with-lease=refs/heads/spill/d4e7ada83c:f583087fcba3deadbeef"
               for a in push), push


def test_publish_lane_ref_fetches_before_checking_the_pipeline(monkeypatch, tmp_path):
    """has_pipeline reads master, so the fetch has to come first or a fresh host is misread."""
    calls = _lane_probe(monkeypatch, tmp_path)
    dsp.publish_lane_ref("hikari", "a" * 40)
    order = [next((k for k in argv if k in ("fetch", "cat-file", "push")), None) for argv, _ in calls]
    assert order.index("fetch") < order.index("cat-file") < order.index("push")


def test_publish_lane_ref_strips_the_proxy(monkeypatch, tmp_path):
    """cnb.cool must be reached directly (the proxy throttles it to ~13 KB/s)."""
    calls = _lane_probe(monkeypatch, tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://daemon.node.local:7890")
    dsp.publish_lane_ref("hikari", "a" * 40)
    for argv, kw in calls:
        if "push" in argv or "fetch" in argv or "ls-remote" in argv:
            assert "env" in kw, f"{argv[3]} must pass an explicit env so the proxy is stripped"
            assert not ({"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"} & set(kw["env"]))


def test_publish_lane_ref_propagates_push_failures(monkeypatch, tmp_path):
    """A failed publish must reach spill() so the run goes to the build lane."""
    _lane_probe(monkeypatch, tmp_path, fail_on="push")
    with pytest.raises(subprocess.CalledProcessError):
        dsp.publish_lane_ref("hikari", "a" * 40)


def test_publish_lane_ref_leaves_no_ref_without_a_pipeline(monkeypatch, tmp_path):
    """No pipeline on the host means the lane cannot run, so nothing may be pushed — a stray
    branch would accumulate on every spill."""
    calls = _lane_probe(monkeypatch, tmp_path, cat_rc=1)
    branch, has_pipeline = dsp.publish_lane_ref("hikari", "a" * 40)
    assert (branch, has_pipeline) == ("spill/" + "a" * 10, False)
    assert not any("push" in argv for argv, _ in calls)


def test_resolve_polls_the_lane_host(monkeypatch):
    """The workspace lives on the host repo and build/status is repo-scoped, so polling the
    target repo returned "Failed to retrieve pipeline" and the run was released unseen."""
    polled = []
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: (
        polled.append(path), {"status": "running",
                              "pipelinesStatus": {"p1": {"stages": [{"name": dsp.WS_CHECK_STAGE,
                                                                    "status": "running"}]}}})[1])
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: 1_000_000.0)}))
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    state = {"7": {"mode": "ws", "host": "ci-infra-hikari", "sn": "sn-1", "repo": "hikari",
                   "sha": "a" * 40, "url": "", "since": 999_999.0}}
    dsp.resolve(state)
    assert polled == ["/celestia-island/ci-infra-hikari/-/build/status/sn-1"]


def test_deferral_does_not_spend_the_batch_budget(monkeypatch):
    """Deferrals used to count as started spills, so a queue head full of already-has-workspace
    repos could burn the whole per-loop budget and starve every other repo."""
    class _Stop(BaseException):
        pass

    entries = [{"repo": "hikari", "run_id": i, "sha": f"{i:040d}"} for i in (1, 2, 3)]
    entries += [{"repo": "arona", "run_id": 4, "sha": "d" * 40},
                {"repo": "evernight", "run_id": 5, "sha": "e" * 40}]
    state = {"_recent": {}, "9": {"mode": "ws", "host": "ci-infra-hikari", "sn": "sn-run",
                                  "repo": "hikari", "sha": "f" * 40, "url": "", "since": 0.0}}
    monkeypatch.setattr(dsp, "THRESHOLD", 0)
    monkeypatch.setattr(dsp, "GH", "g")
    monkeypatch.setattr(dsp, "CNB", "c")
    monkeypatch.setattr(dsp, "FETCH", "f")
    monkeypatch.setattr(dsp, "CNB_WS", "w")
    monkeypatch.setattr(dsp, "load_state", lambda: state)
    monkeypatch.setattr(dsp, "census", lambda: entries)
    monkeypatch.setattr(dsp, "save_state", lambda st: None)
    monkeypatch.setattr(dsp, "resolve", lambda st: None)
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    spilled = []

    def fake_spill(entry, st):
        if entry["repo"] == "hikari":
            return False          # the real contract: False = deferred, do not spend the budget
        spilled.append(entry["run_id"])
        return True

    monkeypatch.setattr(dsp, "spill", fake_spill)
    monkeypatch.setattr(dsp, "time", type("T", (), {
        "sleep": staticmethod(lambda s: (_ for _ in ()).throw(_Stop())),
        "time": staticmethod(lambda: 0.0)}))
    with pytest.raises(_Stop):
        dsp.main()
    # 3 hikari deferrals (spill() -> None) must not consume the budget of 3: arona + evernight land
    assert [i for i in spilled if i][:2] == [4, 5]


def test_a_build_record_does_not_defer_a_workspace_spill(monkeypatch):
    """The deferral key is an *active workspace*, not just any record for the repo."""
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror", lambda repo, sha: "e" * 40)
    monkeypatch.setattr(dsp, "publish_lane_ref", lambda repo, sha: ("spill/" + sha[:10], True))
    started = []
    monkeypatch.setattr(dsp, "ws_start", lambda repo, ref: (started.append(repo), {"sn": "s"})[1])
    state = {"9": {"mode": "build", "sn": "cnb-x", "repo": "hikari", "sha": "a" * 40,
                   "url": "", "since": 0.0}}
    dsp.spill({"repo": "hikari", "run_id": 51, "sha": "b" * 40}, state)
    assert started == ["ci-infra-hikari"]


def test_real_spill_returns_false_when_it_defers(monkeypatch):
    """The main loop keys on `spill(...) is False`, so the real function — not a stub — has to
    return that value, otherwise a deferral silently spends a batch slot again."""
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    monkeypatch.setattr(dsp, "publish_lane_ref",
                        lambda repo, sha: pytest.fail("a deferral must not touch the lane host"))
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: pytest.fail("no build-lane dispatch"))
    state = {"9": {"mode": "ws", "host": "ci-infra-hikari", "sn": "sn-running", "repo": "hikari",
                   "sha": "a" * 40, "url": "", "since": 0.0}}
    assert dsp.spill({"repo": "hikari", "run_id": 61, "sha": "b" * 40}, state) is False


def test_resolve_falls_back_to_the_target_repo_for_legacy_records(monkeypatch):
    """Records written before the lane moved to a host carry no `host` key; they must keep
    polling the repo the workspace really ran on rather than a path built from None."""
    polled = []
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: (
        polled.append(path),
        {"status": "running",
         "pipelinesStatus": {"p1": {"stages": [{"name": dsp.WS_CHECK_STAGE, "status": "running"}]}}})[1])
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: 1_000_000.0)}))
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    state = {"7": {"mode": "ws", "sn": "sn-legacy", "repo": "hikari", "sha": "a" * 40,
                   "url": "", "since": 999_999.0}}
    dsp.resolve(state)
    assert polled == ["/celestia-island/hikari/-/build/status/sn-legacy"]


def test_ws_success_reports_and_cancels_on_the_target_repo(monkeypatch):
    """The verdict belongs to the target repo's SHA even though the workspace ran on the host:
    posting to the host (or cancelling there) would be invisible where it matters."""
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {
        "status": "success",
        "pipelinesStatus": {"p1": {"stages": [{"name": dsp.WS_CHECK_STAGE, "status": "success"}]}}})
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: 1_000_000.0)}))
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    stopped, posted, gh = [], [], []
    monkeypatch.setattr(dsp, "ws_stop", lambda sn: stopped.append(sn))
    monkeypatch.setattr(dsp, "post_status", lambda repo, sha, state_, url: posted.append((repo, sha, state_)))

    def fake_gh(path, data=None, method=None):
        gh.append((path, method))
        return {"status": "queued"} if method is None else {}

    monkeypatch.setattr(dsp, "gh_api", fake_gh)
    state = {"4242": {"mode": "ws", "host": "ci-infra-hikari", "sn": "sn-1", "repo": "hikari",
                      "sha": "a" * 40, "url": "u", "since": 999_999.0}}
    dsp.resolve(state)
    assert posted == [("hikari", "a" * 40, "success")]
    assert stopped == ["sn-1"]
    assert gh == [("/repos/celestia-island/hikari/actions/runs/4242", None),
                  ("/repos/celestia-island/hikari/actions/runs/4242/cancel", "POST")]
    assert state["_recent"]["a" * 40] == 1_000_000.0


def test_ws_failure_keeps_the_farm_run(monkeypatch):
    """A failed gated stage must post a failure on the TARGET repo and leave the queued farm run
    untouched — the daemon promises it never posts a green it did not earn, and cancelling on a
    failure would drop the very signal the author needs."""
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {
        "status": "error",
        "pipelinesStatus": {"p1": {"stages": [{"name": dsp.WS_CHECK_STAGE, "status": "error"}]}}})
    monkeypatch.setattr(dsp, "time", type("T", (), {"sleep": staticmethod(lambda s: None),
                                                    "time": staticmethod(lambda: 1_000_000.0)}))
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    stopped, posted, gh = [], [], []
    monkeypatch.setattr(dsp, "ws_stop", lambda sn: stopped.append(sn))
    monkeypatch.setattr(dsp, "post_status",
                        lambda repo, sha, state_, url: posted.append((repo, sha, state_)))
    monkeypatch.setattr(dsp, "gh_api", lambda path, data=None, method=None: gh.append((path, method)))
    state = {"4242": {"mode": "ws", "host": "ci-infra-hikari", "sn": "sn-1", "repo": "hikari",
                      "sha": "a" * 40, "url": "u", "since": 999_999.0}}
    dsp.resolve(state)
    assert posted == [("hikari", "a" * 40, "failure")]
    assert gh == []                       # no run lookup, no cancel
    assert stopped == ["sn-1"]
    assert "4242" not in state


def test_every_watched_cargo_repo_is_on_the_lane():
    """The lane carries every cargo repo the census watches. The two non-cargo repos
    (celestia-devtools = python-check, evernight-appliance = webui-check) need their own
    pipeline variants and are deliberately not in this set."""
    assert dsp.DEV_QUOTA == {"shittim-chest", "evernight", "arona", "hikari",
                             "plana", "entelecheia", "malkuth", "kirino"}
    assert dsp.DEV_QUOTA <= set(dsp.WATCHED)
    assert set(dsp.WATCHED) - dsp.DEV_QUOTA == {"celestia-devtools", "evernight-appliance"}


@pytest.mark.parametrize("repo", ["plana", "entelecheia", "malkuth", "kirino"])
def test_wave2_repos_route_to_their_lane_host(monkeypatch, repo):
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror", lambda r, sha: "e" * 40)
    monkeypatch.setattr(dsp, "publish_lane_ref", lambda r, sha: (f"spill/{sha[:10]}", True))
    started = []
    monkeypatch.setattr(dsp, "ws_start", lambda r, ref: (started.append((r, ref)), {"sn": "s"})[1])
    state = {}
    dsp.spill({"repo": repo, "run_id": 71, "sha": "b" * 40}, state)
    assert state["71"]["mode"] == "ws"
    assert state["71"]["host"] == f"ci-infra-{repo}"
    assert started == [(f"ci-infra-{repo}", "spill/" + "b" * 10)]
