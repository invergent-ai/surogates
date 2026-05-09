# Hermes Tier 2 Port Plan

## Scope

Port the Hermes Tier 2 production hardening items that map cleanly to
Surogates, with tests and small commits per group. Direct ports are avoided
where Surogates already has a stronger local abstraction.

## Checklist

1. [x] Add response-side surrogate sanitization before persisted LLM responses.
2. [x] Add URL safety always-blocked cloud metadata floor.
3. [x] Skip credential pool rotation when only one available credential exists.
4. [x] Invalidate file read dedup entries on writes/patches and bound read tracker data.
5. [ ] Add fuzzy-match escape-drift detection.
6. [ ] Make V4A multi-file patch application two-phase.
7. [ ] Add thread-safety snapshots/locking to the tool registry.
8. [ ] Port MCP OAuth state manager behavior in Surogates form: disk mtime reload, expiry seeding, 401 dedup.
9. [ ] Add MCP auth/session recovery and circuit breaker behavior.
10. [ ] Add OSV malware scan for tenant/admin stdio MCP package launches.
11. [ ] Add Redis-backed cross-session provider rate-limit guard.
12. [ ] Add small tenant-aware auxiliary client path for context compression.
13. [ ] Add image-too-large detection and retry-by-shrinking image data URLs.
14. [ ] Add configurable tool output limit knobs.
15. [ ] Audit path traversal validators and consolidate on existing workspace sandbox utilities where appropriate.

## Implementation Notes

- Use TDD for each behavior: write focused failing tests, then implementation,
  then focused verification.
- Commit after each coherent group.
- Keep unrelated dirty files untouched.
