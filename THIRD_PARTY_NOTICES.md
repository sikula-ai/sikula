# Third-Party Notices

Sikula is licensed under AGPL-3.0-only. This document lists notable third-party
software and data referenced by the repository.

## Runtime Dependencies

| Component | License | Usage |
|---|---|---|
| PyYAML | MIT | YAML config parsing |
| python-dotenv | BSD-3-Clause | Loading project `.env` files |

## Development and Build Tooling

The repository uses common development tools such as `setuptools`, `wheel`,
`pytest`, `pytest-cov`, and `ruff` for packaging, testing, coverage, and code
style checks. These tools are not bundled into Sikula's runtime package.

## Example Projects

| Component | License | Usage |
|---|---|---|
| Gradle Wrapper files | Apache-2.0 | Included in Android and JVM/Gradle examples |
| Maven Wrapper files | Apache-2.0 | Included in the JVM/Maven example |
| REST Countries data subset | MPL-2.0 | Static example country datasets used by the example projects |

The example projects also declare their own build and test dependencies through
Gradle, Maven, Cargo, and Xcode project files. Those dependencies are used only
when running the examples.

If you redistribute Sikula or the example projects, review the applicable
third-party license terms for the specific package or artifact you distribute.
