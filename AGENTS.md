# Project instructions for agents

## Release the robot — always build distributions too

When the user asks to release a new version of the robot, ALWAYS build the
distributions locally in the same release run — not just bump the version and
push a tag.

Follow `docs/release/build-and-distribution.md` without skipping its checks.
The release is complete only when the macOS and Windows ZIP distributions,
release notes and checksums are available from the GitHub Release.

Release contract details live in `openspec/specs/release/spec.md`.

## General

- Working code is in `src/`, tests in `tests/` (pytest).
- Test layout: `tests/unit/` for fast isolated unit tests, `tests/snapshot/` for
  snapshot tests; test files directly in `tests/` root are forbidden (except
  `conftest.py`).
- Russian is the primary language for commit messages and user-facing output.
