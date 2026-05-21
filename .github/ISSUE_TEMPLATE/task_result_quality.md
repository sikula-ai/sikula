---
name: Task result quality
about: Sikula finished, but the produced result was not good enough
labels: feedback, quality
---

**What did you ask Sikula to do?**
Paste the task description or a shortened version of it.

**What happened?**
Describe the result Sikula produced.

**What was wrong or missing?**
Explain what made the output not ready for human review.

**Project context**
- Stack/platform:
- Build tool:
- Provider / model:
- Sikula version (`sikula --version`):
- Pipeline phases enabled, if customized:

**Did the pipeline pass?**
- Did Sikula finish successfully?
- Did build/tests/checks pass?
- Did reviewer/security reviewer approve?

**Useful context**
If possible, include:

- The final diff summary (`git diff --stat main...<sikula-branch>`)
- Relevant reviewer/security reviewer findings
- Relevant build/test/check output
- Screenshots for UI output

**Before sharing task state:** do not attach raw `.sikula/state/*.json` files without reviewing them first. State files may contain prompts, source excerpts, build logs, and other sensitive project data.
