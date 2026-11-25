# Agent Workflow Convention

These four rules are the org-wide agent-workflow convention. They live here,
in the shared devtools repository, so every downstream repository's `AGENTS.md`
can cite them instead of re-deriving them per repository: the wording, the
rationale, and the failure each rule prevents stay in one place, and a
repository's `AGENTS.md` only adds the concrete commands its stack needs.

Each rule is written from a failure observed in a real session, not from
theory. They are mandatory for every agent that opens a branch, creates a
pull request, or dispatches a subagent.

## 1. One worktree, one branch — never reuse

Every unit of work gets a fresh `git worktree` and a fresh branch. Never
switch an existing worktree to a second branch for a follow-up wave, even
when its `node_modules/` or `target/` is warm.

The install cost is cheaper than stale-branch state, half-applied merges, and
wrong-branch confusion. A warm directory saves minutes; a commit made on the
wrong branch can cost a rewrite, a re-review, and a mixed history that no
longer explains itself. Reusing a worktree also hides which branch a command
actually ran on, so every later step (lint, commit, push, `gh pr create`)
inherits the ambiguity.

Create the next worktree instead — a sibling directory, one branch per
directory:

```bash
git worktree add ../<repo>-wt-<task> -b <type>/<name> master
```

The dependencies are reinstalled there (for pnpm the global store keeps it
fast); a cold directory is the price of a clean branch.

## 2. PR numbers are captured, never guessed

When a workflow chains create → merge, capture the PR number (or URL) from
the `gh pr create` output and merge THAT exact number. Never hardcode a
guessed number in a later command: a concurrent session can take the adjacent
number, and the merge lands on the wrong PR.

This was observed in practice — a cache-mesh PR was merged this way by
mistake. A number copied from a plan, an issue, or a sibling PR is not a
claim; it is only authoritative when it comes out of the command that created
the PR.

```bash
pr_url=$(gh pr create --title "..." --body "...")
pr_number=${pr_url##*/}
gh pr merge "$pr_number" --squash --subject "..."
```

Capture the URL rather than only the digits: it keeps the repository in view
and the number is its last path segment. The merge command consumes that
value, never a number remembered from an earlier step.

## 3. Same-domain subagents go out serially

Dispatching two agents with overlapping file ownership in one round collides
in the same worktree and burns one agent as a stand-down verifier. Parallel
dispatch is for provably disjoint ownership; same-area tasks go out one at a
time, each brief naming its own distinct worktree path.

"Provably disjoint" means the file lists can be compared before dispatch and
do not intersect. If two briefs touch the same files, the same module, the
same lockfile, or the same generated artifact, they are same-domain work and
go out serially.

Each brief names its own worktree path, and a follow-up wave gets a new
worktree (rule 1) rather than the previous wave's directory.

## 4. Release lanes own the version slot

A version number is a release-lane property, not an edit riding a feature PR.
The version slot — the manifest field a release publishes — changes only in a
release pull request whose subject is `⬆️ Release <product> X.Y.Z.`, one
release PR per version, merged before its tag is pushed. A bump inside a
feature PR ships a number nobody decided on, on the merge that happened to
carry it, whether or not anything was published.

Semantics: a minor bump marks a wave or a breaking change; a patch bump is
another release of the current line. Removing, renaming, or splitting a public
export is breaking even on `0.x`, and it bumps the minor — never a patch. The
semver signal is what a downstream pins and what an automated upgrade decides
from, so a contract change smuggled into a patch breaks every consumer that
accepted the previous patch on the promise that it could.

Tags are part of the release. Every published version carries its pushed tag
(`v*` for npm, `cargo-v*` for crates). A `workflow_dispatch` publish that skips
the tag ships a version nobody can reproduce: publish only from a tag, or make
the publish job create and push the tag it released. The tag is the coordinate
that ties the registry's bytes to a commit; without it the only record of what
shipped is the registry itself, which cannot be rebuilt, bisected, or patched.

Lanes are append-only. Never re-number, re-publish, or reuse a version. Numbers
already burned by a failed dispatch are gone; document the skip in the release
notes instead of back-filling, and never let the tag and the published version
disagree. A registry keeps one artifact per version string, so a back-fill does
not repair the lane — it makes one number mean two trees forever.

Release on demand, not per merge. A release exists so downstreams can pin it.
Batch a release when a change is picked up (or a wave lands), not after every
merge; a per-merge cadence turns the patch digit into a build counter and makes
the semver signal unreadable — a consumer cannot tell a one-line fix from a
breaking rewrite, and neither can an agent reading the registry.

Observed failure (hikari, 2026-11): the 0.55 line ran 450 npm publishes in 70
days (up to 21/day), 73 publishes never got a tag, 36 patch numbers were burned
by dispatch typos. It also shipped seven public-surface removals and splits
inside patch releases: `git log --diff-filter=D -- packages/vue/src/components`
names seven commits in that window, five of them component modules
(`HkSlideConfirm`, `HkSegmentedRadio`, the height-ride family, the chat kit, the
token field). `celestia-devtools release-audit` reconciles a
repository's tags against its registries and fails on exactly this drift.

## Adoption

Downstream repositories cite this document from their `AGENTS.md` as the
org-wide source of these rules, and add the concrete commands for their own
stack. Rule 4's drift is machine-checkable: `celestia-devtools release-audit
--repo . --npm <package>` reconciles the repository's tags with the registry,
and `.github/workflows/release-audit.yml` is the reusable caller. When a rule
changes, it changes here first, and the citing files follow.
