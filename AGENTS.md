# Project instructions for agents

## Release the robot — always build distributions too

When the user asks to release a new version of the robot, ALWAYS build the
distributions locally in the same release run — not just bump the version and
push a tag.

Follow `docs/release/build-and-distribution.md` without skipping its checks.
The release is complete only when the macOS and Windows ZIP distributions,
release notes and checksums are available from the GitHub Release.

Release contract details live in `openspec/specs/release/spec.md`.

## Secrets — never print them (strict, no exceptions)

Tokens, passwords and secret addresses (`TINKOFF_TOKEN`, `TELEGRAM_BOT_TOKEN`,
`CLOUDFLARE_URL` and its host, `MARKET_DATA_EXPORT_TOKEN`,
`MARKET_STRUCTURE_REDIS_PASSWORD`, any future value from `.env`) MUST NOT
appear in any output where they could leak:

- logs (`bot_debug.log`, audit/trace logs), console, Telegram messages, trade
  journal, CSV/JSON exports, reports, diagnostics and backup files;
- exception messages and tracebacks — never log or print a raw `requests`/HTTP
  exception or a URL that embeds a token; log the exception type instead;
- tests, fixtures, snapshots and docs — use obviously fake values only;
- commit messages, PR descriptions, chat replies and tool output you show the
  user — report counts or `[скрыто]`, never the value.

Rules for code:

- Every new secret is added to `secrets=` of `setup_logging` in `run.py`, so
  the log formatter replaces it with `[скрыто]`. This is a safety net, not a
  licence: do not pass secrets into log calls in the first place.
- Never build a log message, `print`, notification text or saved file from a
  value that contains a secret (full API URL with token, headers, `.env`
  contents, client objects).
- Do not lower masking or log third-party HTTP/gRPC request details with
  secrets; a new dependency that logs URLs or headers needs a test proving the
  secret is masked.
- When reading `.env` or logs while debugging, never echo the values; compare
  or count them in a script.

If a secret has leaked anywhere, say so to the user immediately and recommend
revoking/rotating it.

## General

- Working code is in `src/`, tests in `tests/` (pytest).
- Test layout: `tests/unit/` for fast isolated unit tests, `tests/snapshot/` for
  snapshot tests; test files directly in `tests/` root are forbidden (except
  `conftest.py`).
- Russian is the primary language for commit messages and user-facing output.
