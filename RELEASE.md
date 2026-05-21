# Release checklist

Steps to follow before every release.

## 1. Update version

In `pyproject.toml`, bump `version` according to what changed:
- Bug fixes only → PATCH (`0.1.0` → `0.1.1`)
- New features, backward compatible → MINOR (`0.1.0` → `0.2.0`)
- Breaking changes to CLI or config format → MAJOR (`0.x.y` → `1.0.0`)

## 2. Update changelog

In `CHANGELOG.md`, move release-ready entries from `[Unreleased]` into the release section.
For the first public release, replace `TBD` in `[0.1.0] - TBD` with the release date.

## 3. Run tests

```bash
python3 -m pytest tests/
```

## 4. Check code style

```bash
ruff check .
ruff format --check .
```

## 5. Commit and tag

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "Release vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

## 6. Publish to PyPI (when ready)

```bash
pip install build twine
python -m build
```

Verify the built package locally before uploading:

```bash
pipx install dist/sikula-X.Y.Z-py3-none-any.whl
sikula --version
pipx uninstall sikula
```

Upload to PyPI:

```bash
twine upload dist/*
```

## 7. Verify install from PyPI

```bash
pipx install sikula
sikula --version
```
