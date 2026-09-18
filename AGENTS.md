# Project instructions for agents

## Release the robot — always build distributions too

When the user asks to release a new version of the robot, ALWAYS build the
distributions locally in the same release run — not just bump the version and
push a tag.

Release process:

1. Bump `__version__` in `src/__init__.py` (SemVer MAJOR.MINOR.PATCH; rules are
   written as a comment in that file).
2. In `CHANGELOG.md`, rename `## Unreleased` to `## <version> — <dd.mm.yyyy>` and
   add a fresh `## Unreleased` header. Keep notes user-readable.
3. Verify: `.venv/bin/python -m pytest -q` (tests) and
   `.venv/bin/python -m ruff check .` (lint; pre-existing errors in `tools/` and
   `openspec/_check_sync.py` are tolerated).
4. Commit, create tag `v<X.Y.Z>`, push the tag and the branch. Pushing the tag
   triggers the GitHub Actions `build-release` workflow (Windows binary).
5. Build the local (macOS) distribution with PyInstaller:
   ```
   PYINSTALLER_NAME=robot-v<X.Y.Z>_macOs .venv/bin/python -m PyInstaller run.spec \
       --noconfirm --distpath dist --workpath build
   ```
   Binary name must follow the CI convention: `robot-v<X.Y.Z>_macOs`
   (Windows: `robot-v<X.Y.Z>_win.exe`, built by CI on tag push).
6. Write release notes next to the binary at `dist/robot-v<X.Y.Z>.txt` (тезисы
   изменений версии, понятные тому, кто запускает робота).

Release contract details live in `openspec/specs/release/spec.md`.

## General

- Working code is in `src/`, tests in `tests/` (pytest).
- Test layout: `tests/unit/` for fast isolated unit tests, `tests/snapshot/` for
  snapshot tests; test files directly in `tests/` root are forbidden (except
  `conftest.py`).
- Russian is the primary language for commit messages and user-facing output.