# Add current-branch mode for review fixes

## Goal

Allow operators who are already on the branch they want Sikula to fix to run review fixes safely without checking that same branch out into a second git worktree.

The new mode must use the currently checked-out branch as both the review target and final delivery branch while preserving Sikula's isolation, resumability, inspectability, and auditability guarantees.

## Scope

- Add support for `sikula review --fix --current-branch --base-branch <base> --description-file <file>`.
- Treat the currently checked-out git branch as the review branch and final delivery target in current-branch mode.
- Make `--current-branch` valid only with `sikula review --fix`.
- Make `--branch` and `--current-branch` mutually exclusive.
- Preserve the existing `--branch` requirement for non-current-branch review flows.
- Require the operator's current worktree to be clean before review or fix work starts.
- Compute the review diff against `<base>...HEAD`.
- Run write-capable review-fix work inside an isolated Sikula task worktree.
- Avoid creating a second worktree that checks out the current target branch directly.
- Allow the isolated worktree to use detached `HEAD` at the target branch's starting commit or an internal Sikula task branch.
- Keep the operator's current branch unchanged until successful finalization.
- Commit successful fixes in the isolated Sikula worktree before delivery.
- Deliver successful fixes back to the originally current branch without switching branches.
- Preserve normal review-fix resumability through Sikula task state.
- Add explicit task state metadata so resume can distinguish normal branch-worktree review fixes from current-branch delivery tasks and verify that continuing is safe.

## Acceptance criteria

- `sikula review --fix --current-branch --base-branch <base> --description-file <file>` is accepted by the CLI.
- `--current-branch` without `--fix` is rejected clearly.
- `--branch` together with `--current-branch` is rejected clearly.
- Current-branch mode uses the current branch name as the review branch.
- The command fails before starting review or fix work when `HEAD` is detached.
- The command fails before starting review or fix work when the current branch cannot be determined.
- The command fails before starting review or fix work when the current worktree has staged changes.
- The command fails before starting review or fix work when the current worktree has unstaged changes.
- The command fails before starting review or fix work when the current worktree has untracked files.
- The command fails before starting review or fix work when the base branch or ref cannot be resolved.
- The initial review diff is computed with `<base>...HEAD`.
- Current-branch mode creates an isolated Sikula task worktree without checking out the target branch directly.
- Current-branch mode does not run branch checkout or branch switch commands.
- The operator's branch and worktree remain unchanged while agents are running.
- The normal review-fix flow still runs: reviewer, optional security reviewer, implementer fixes, validation, and re-review.
- Successful fixes are committed in the isolated Sikula worktree first.
- After a successful isolated fix run, Sikula fast-forwards or otherwise safely applies the fix commit to the originally current branch during finalization.
- Delivery fails clearly and preserves the isolated worktree for inspection or resume if the operator's current worktree is no longer clean.
- Delivery fails clearly and preserves the isolated worktree for inspection or resume if the operator is no longer on the same target branch.
- Delivery fails clearly and preserves the isolated worktree for inspection or resume if the target branch has moved since the task started.
- Delivery fails clearly and preserves the isolated worktree for inspection or resume if the fix commit cannot be fast-forwarded or otherwise safely applied.
- Interrupted current-branch review-fix tasks remain resumable through `sikula run --task-id <id>`.
- Existing `sikula review --fix --branch <branch>` behavior remains unchanged.
- Existing report-only `sikula review --branch <branch>` behavior remains unchanged.
- Existing review-fix task resume behavior for isolated worktrees remains unchanged.
- Existing cleanup and delete behavior for isolated worktree tasks remains valid for current-branch review-fix tasks.

## Out of scope

- The command must not switch branches.
- The command must not push.
- The command must not create pull requests.
- The command must not mutate remotes.
- Existing non-current-branch review and review-fix behavior must not be redesigned.

## Context

- `sikula review --fix` currently creates a separate git worktree by checking out the reviewed branch.
- Checking out the reviewed branch into a second worktree is awkward or fails when the operator is already on that branch, because git cannot check out the same branch into a second worktree.
- CLI parsing for `review` currently requires `--branch`; this should become mutually exclusive with `--current-branch` while preserving the existing requirement for non-current-branch review.
- `cmd_review` in `sikula.py` currently creates `.sikula/worktrees/<task-id>` for both report-only and fix mode.
- Current-branch mode should still create an isolated Sikula task worktree, but it must not attempt `git worktree add <path> <current-branch>`.
- `_finalize_worktree` currently commits changes and removes the isolated worktree.
- Current-branch mode needs a finalization path that commits fixes in the isolated worktree, verifies the original target branch is still safe, fast-forwards the target branch to include the fix commit, and only then removes the isolated worktree.
- `cmd_run --task-id` already handles `review_mode == "review_fix"` resumes.
- Useful current-branch task state metadata includes delivery mode, target branch name, starting target `HEAD`, isolated fix commit, and delivery status or result.
- `core/orchestrator.py` refreshes review-fix diffs from `merge-base(base, HEAD)` and `git diff <merge-base>`; this should continue to work from inside the isolated current-branch review-fix worktree.

## Test expectations

- Cover CLI acceptance for `sikula review --fix --current-branch --base-branch <base> --description-file <file>`.
- Cover CLI rejection for `--current-branch` without `--fix`.
- Cover CLI rejection for `--branch` together with `--current-branch`.
- Cover use of the current branch name as the review branch.
- Cover detached `HEAD` failure.
- Cover dirty worktree failures for staged-only, unstaged, and untracked changes.
- Cover unresolved base branch or ref failure.
- Cover initial review diff computation with `<base>...HEAD`.
- Cover isolated Sikula task worktree creation without checking out the target branch directly.
- Cover that current-branch mode does not run branch checkout or switch commands.
- Cover that the operator's branch and worktree are unchanged while agents are running.
- Cover committing successful fixes in the isolated worktree.
- Cover fast-forwarding or safely applying the successful fix commit to the originally current branch during finalization.
- Cover preserving the isolated worktree when final delivery cannot be completed safely.
- Cover resumability through `sikula run --task-id <id>`.
- Keep existing branch-worktree review-fix behavior covered.

## Reviewer focus

- Inspect the current-branch review-fix safety boundary: CLI validation for --current-branch, clean current-worktree and branch/ref preflights, worktree creation that does not check out the active target branch, diff computation against <base>...HEAD, isolated commit creation, and final delivery back to the original branch only when the operator is still on the same clean branch and the target has not moved. Also verify TaskState additions preserve resume/audit compatibility, failed delivery preserves the isolated worktree, existing --branch review/report flows remain unchanged, reviewer and security reviewer stay read-only, and tests cover the new branch, resume, cleanup/delete, and failure paths.

## Notes

- Reviewer focus: Supported by the draft plus repository invariants in guidelines.md, ARCHITECTURE.md, docs/review.md, sikula.py, core/state.py, and core/orchestrator.py.

## Project context

- Stack: Python / python

## Validation

- `python3 -m compileall -q agents/ core/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
