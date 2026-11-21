# Agent Workflow Convention

These three rules are the org-wide agent-workflow convention. They live here,
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

## Adoption

Downstream repositories cite this document from their `AGENTS.md` as the
org-wide source of these rules, and add the concrete commands for their own
stack. When a rule changes, it changes here first, and the citing files
follow.
