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
- removes the review worktree on completion

## Fix Mode

```bash
sikula review \
  --branch feature/login \
  --base-branch main \
  --description-file pr.md \
  --fix
```

`--fix` applies accepted fixes through the normal orchestrator loop. The planner is disabled, reviewer is enabled, and successful fixes are committed back to the reviewed branch with a Sikula commit message.

If the task fails, the worktree is preserved under `.sikula/worktrees/<task-id>/` and can be resumed:

```bash
sikula run --task-id <task-id>
```

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

Use `security.context` and `security_reviewer.extra_rules` in `.sikula/config.yaml` to make the security review more project-specific.

## CI Use

Report-only review exits with a status code, so it can be used as a CI gate. A common CI command shape is:

```bash
sikula review \
  --branch "$HEAD_BRANCH" \
  --base-branch "$BASE_BRANCH" \
  --description-file pr-description.md
```

Make sure the CI environment has the target branches fetched and the selected provider authenticated.
