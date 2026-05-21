# Security Policy

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues or
public Discussions.

Use GitHub's private vulnerability reporting feature from the repository
Security tab.

If the private reporting flow is unavailable, email `contact@sikula.ai` and
include enough detail to reproduce and assess the issue.

Useful information includes:

- Affected Sikula version or commit
- Operating system and Python version
- Provider/model configuration, if relevant
- Steps to reproduce
- Impact and expected severity
- Any relevant logs or command output

Do not include raw `.sikula/state/*.json` files unless you have reviewed and
redacted them first. State files may contain prompts, source excerpts, build
logs, and other sensitive project data.
