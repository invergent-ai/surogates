# Proxied typed answers: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A caller that sets `answers_open_question` can answer an open question with a typed reply on the API path.

**Architecture:** Add an optional field to `SendMessageRequest` and widen one condition in `send_message` (`surogates/api/routes/sessions.py`).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Redis pub/sub, pytest with testcontainers (Docker).

**Spec:** `docs/superpowers/specs/2026-10-29-proxied-typed-answers-design.md`.

## Global Constraints

- Worktree `/home/monica/invergent/.worktrees/sur-L`, branch `fix/proxied-typed-answers`. NEVER git stash. Don't touch other worktrees or running processes.
- Tests: `PYTHONPATH=$PWD /home/monica/invergent/surogates/.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" -m "not slow" <paths>` (Docker test containers).
- Each guard's test fails without it (mutation-check with cp aside/back, never git checkout).
- Explicit staging; Conventional Commit titles; no AI attribution or Co-Authored-By; no private ids; no em or en dashes.

## Review Focus

- A caller that does NOT send the flag on `/v1/api/` must behave exactly as today (normal message, question still pending).
- With the flag, every other guard still holds: attachments/images, a built-in command, no pending question, a stale question, an inactive session, a message the route writes itself (`extra`).

---

### Task 1: An opt-in flag lets a proxied typed reply answer the open question

**Files:** `surogates/api/routes/sessions.py` (`SendMessageRequest` ~336, the condition ~1291 and its comment). Tests: a route-level test file (the old `tests/test_send_message_pending_question.py` was deleted in "cleanup tests"; find the harness `tests/integration/test_api.py` uses for `/v1/api/sessions/{id}/messages` with a service-account token, and `tests/test_interactive_input.py` for pending-question setup).

- [ ] Failing tests (on `/v1/api/sessions/{id}/messages`, service-account token, a pending ask_user_question): flag set -> answered (an ASK_USER_QUESTION_RESPONSE event, no normal message); flag absent -> unchanged; flag set with an attachment / a built-in command / no pending question -> as without the flag. The web path still answers as today.
- [ ] Implement: `answers_open_question: bool = False` with a description; condition `(not path.startswith("/v1/api/") or body.answers_open_question)`, other guards unchanged; update the comment. Run task tests + `tests -k "message or question or interactive"`; mutation-check; commit `feat(api): let a chat proxy's typed reply answer the open question`.
