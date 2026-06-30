# Add current-branch mode for review fixes

## Problem

`sikula review --fix` currently creates a separate git worktree by checking out the reviewed branch. That is awkward or fails when the operator is already on the branch they want Sikula to fix, because git cannot check out the same branch into a second worktree.

Operators need a safe mode that treats the currently checked-out branch as the review target and final delivery branch while preserving Sikula's existing isolation, resumability, and inspectability guarantees.

## User-facing behavior

Add support for:

```sh
sikula review --fix --current-branch --base-branch <base> --description-file <file>
```

The command should use the currently checked-out git branch as the review branch and final delivery target.

## Requirements

- `--current-branch` is valid only with `sikula review --fix`.
- In current-branch mode, `--branch` is not required and should not be accepted together with `--current-branch`.
- The current worktree must be clean before Sikula starts review or fix work.
- The command must fail clearly before starting if:
  - HEAD is detached.
  - The current branch cannot be determined.
  - The current worktree has staged changes, unstaged changes, or untracked files.
  - The base branch or ref cannot be resolved.
- The review diff must be computed against `<base>...HEAD`.
- Sikula must still run the write-capable review-fix work inside an isolated Sikula task worktree.
- Current-branch mode must not create a second worktree that checks out the current target branch directly.
- The isolated worktree may use detached `HEAD` at the target branch's starting commit or an internal Sikula task branch.
- The operator's current branch must remain unchanged until successful finalization.
- The normal review-fix flow must still run:
  - reviewer
  - optional security reviewer
  - implementer fixes
  - validation
  - re-review
- Successful fixes must be committed in the isolated Sikula worktree first.
- After a successful isolated fix run, Sikula must deliver the fix back to the originally current branch without switching branches.
- Delivery must fail clearly and preserve the isolated worktree for inspection/resume if:
  - the operator's current worktree is no longer clean.
  - the operator is no longer on the same target branch.
  - the target branch has moved since the task started.
  - the fix commit cannot be fast-forwarded or otherwise safely applied.
- The command must not switch branches.
- The command must not push.
- The command must not create pull requests.
- The command must not mutate remotes.
- If interrupted, the task must remain resumable through normal Sikula task state.

## Compatibility

- Existing `sikula review --fix --branch <branch>` behavior must remain unchanged.
- Existing report-only `sikula review --branch <branch>` behavior must remain unchanged.
- Existing review-fix task resume behavior for isolated worktrees must remain unchanged.
- Existing cleanup/delete behavior for isolated worktree tasks must remain valid for current-branch review-fix tasks.

## Implementation notes from repository analysis

- CLI parsing for `review` currently requires `--branch`; it should become mutually exclusive with `--current-branch` while preserving the existing requirement for non-current-branch review.
- `cmd_review` in `sikula.py` currently creates `.sikula/worktrees/<task-id>` for both report-only and fix mode. Current-branch mode should still create an isolated Sikula task worktree, but it must not attempt `git worktree add <path> <current-branch>` because that fails when the branch is already checked out.
- `_finalize_worktree` currently commits changes and removes the isolated worktree. Current-branch mode needs a finalization path that commits fixes in the isolated worktree, verifies the original target branch is still safe, fast-forwards the target branch to include the fix commit, and only then removes the isolated worktree.
- `cmd_run --task-id` already handles `review_mode == "review_fix"` resumes. Current-branch mode needs explicit state metadata so resume can distinguish normal branch-worktree review fixes from current-branch delivery tasks and can verify it is safe to continue.
- Useful state metadata should include the delivery mode, target branch name, starting target `HEAD`, isolated fix commit, and delivery status/result.
- `core/orchestrator.py` refreshes review-fix diffs from `merge-base(base, HEAD)` and `git diff <merge-base>`. This should continue to work from inside the isolated current-branch review-fix worktree.

## Test expectations

Add focused tests that cover:

- CLI accepts `sikula review --fix --current-branch --base-branch <base> --description-file <file>`.
- CLI rejects `--current-branch` without `--fix`.
- CLI rejects `--branch` together with `--current-branch`.
- Current-branch mode uses the current branch name as the review branch.
- Current-branch mode fails on detached HEAD.
- Current-branch mode fails on dirty worktree, including staged-only, unstaged, and untracked changes.
- Current-branch mode fails when the base branch or ref cannot be resolved.
- Current-branch mode computes the initial review diff with `<base>...HEAD`.
- Current-branch mode creates an isolated Sikula task worktree without checking out the target branch directly.
- Current-branch mode does not run branch checkout/switch commands.
- Current-branch mode leaves the operator's branch/worktree unchanged while agents are running.
- Current-branch mode commits successful fixes in the isolated worktree.
- Current-branch mode fast-forwards or safely applies the successful fix commit to the originally current branch during finalization.
- Current-branch mode preserves the isolated worktree when final delivery cannot be completed safely.
- Current-branch mode preserves resumability through `sikula run --task-id <id>`.
- Existing branch worktree review-fix behavior remains covered.
