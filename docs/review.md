# Reviewing Branches

`sikula review` runs Sikula's reviewer and security reviewer against an existing branch. Use it when code already exists and you want an independent quality/security gate.

## Report-Only Review

```bash
sikula review \
  --branch feature/login \
  --base-branch main \
  --description "Add login screen with JWT authentication"
```

Or pass a PR description from a file:

```bash
sikula review \
  --branch feature/login \
  --base-branch main \
  --description-file pr.md
```

Report-only review:

- creates a detached worktree for review
- computes `git diff base...branch`
- runs `ReviewerAgent`
- runs `SecurityReviewerAgent` if review passes and security review is enabled
- prints a review summary
- exits `0` when approved and `1` when issues are found
- removes the review worktree on completion, provider failure, or interruption,
  including interruption during optional referenced-file enrichment

Report-only review state is kept for audit with `sikula show <task-id>`, but it
is not resumable. If a report-only review fails or is interrupted, re-run
`sikula review` to start a fresh review. While the report-only review process is
still running, `sikula status` reports `wait` instead of suggesting a duplicate
review run.

## Fix Mode

```bash
sikula review \
  --branch feature/login \
  --base-branch main \
  --description-file pr.md \
  --fix
```

`--fix` applies accepted fixes through the normal orchestrator loop. The planner is disabled, reviewer is enabled, and successful fixes are committed back to the reviewed branch with a Sikula commit message.

If `review --fix` is interrupted, the worktree is preserved under `.sikula/worktrees/<task-id>/` and can be resumed:

```bash
sikula run --task-id <task-id>
```

If the task reaches terminal failed state, reset the failed marker before retrying:

```bash
sikula run --task-id <task-id> --reset-failed
```

## Current-Branch Fix Mode

Use current-branch mode when you are already on the branch Sikula should review
and fix:

```bash
sikula review \
  --fix \
  --current-branch \
  --base-branch main \
  --description-file pr.md
```

`--current-branch` is valid only with `--fix` and is mutually exclusive with
`--branch`. Sikula uses the currently checked-out branch as the review target
and final delivery target.

Before agents start, Sikula fails clearly if:

- `HEAD` is detached or the current branch cannot be determined
- the current worktree has staged, unstaged, or untracked changes
- the base branch or ref cannot be resolved

Current-branch fix mode still keeps write-capable agent work out of the
operator's checkout. Sikula records the current branch and starting `HEAD`,
creates an isolated detached worktree at that commit, computes the initial diff
as `<base>...HEAD`, and runs the normal review-fix orchestrator loop there. It
does not switch branches and does not check out the target branch into a second
worktree.

On success, Sikula commits fixes inside the isolated worktree first. It then
rechecks that the operator is still on the original branch, the operator's
worktree is clean, and the branch `HEAD` is still the recorded starting commit.
Only then does it deliver with a safe fast-forward. Sikula does not push, create
pull requests, or mutate remotes.

If delivery cannot be completed safely, the isolated worktree is preserved and
the task remains retryable:

```bash
sikula run --task-id <task-id>
```

If the preserved worktree is intentionally removed with `sikula cleanup --force`,
the task remains available for audit through `sikula show <task-id>`, but the
delivery can no longer be retried from that state.

Use `--reset-failed` only after a terminal failed state, as with other
`review --fix` tasks.

After a review-fix produces and delivers a publishable commit, its PR-ready
handoff can be rendered without exposing the full audit state:

```bash
sikula summary <task-id>
```

Report-only review tasks are intentionally rejected because they do not
represent an implementation result. No-change review-fix tasks do not have a
commit to publish and are also rejected.

## Review Context

`--description` or `--description-file` is required. Treat it like a PR description: explain what changed, why it changed, and what reviewers should care about.

If the description names local files such as screenshots, mockups, specs, PDFs, or spreadsheets, Sikula tries to find and inline those files into the review prompt. If none are found, review continues normally.

## Security Review

Security review is controlled by the project config and can be overridden:

```bash
sikula review \
  --branch feature/login \
  --description-file pr.md \
  --security-review

sikula review \
  --branch feature/login \
  --description-file pr.md \
  --no-security-review
```

Review mode honors `reviewer.extra_rules` and `security_reviewer.extra_rules`. In `--fix` mode, `test_writer.extra_rules` is also used when the test writer runs. Rule files for agents that run, and any `guidelines.context_files`, must be tracked, clean, and present as files in the reviewed branch before Sikula creates the review worktree. See [Project-Specific Agent Rules](configuration.md#project-specific-agent-rules).

## CI Use

Report-only review exits with a status code, so it can be used as a CI gate. A common CI command shape is:

```bash
sikula review \
  --branch "$HEAD_BRANCH" \
  --base-branch "$BASE_BRANCH" \
  --description-file pr-description.md
```

Make sure the CI environment has the target branches fetched and the selected provider authenticated.
