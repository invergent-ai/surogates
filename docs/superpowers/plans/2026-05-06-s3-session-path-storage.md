# S3 Session Path Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store S3 session workspaces in one per-agent bucket under `sessions/{session_id}/`.

**Architecture:** Add centralized agent bucket/prefix helpers and a backend method for resolving the visible workspace path. Update session creation, workspace APIs, artifacts, skill staging, cleanup, and Kubernetes sandbox resources to derive session object keys from `session_id` instead of using per-session buckets.

**Tech Stack:** Python, FastAPI, Pydantic settings, async storage backends, Kubernetes sandbox manifests, pytest.

---

### Task 1: Storage Helpers

**Files:**
- Modify: `surogates/storage/tenant.py`
- Modify: `surogates/storage/backend.py`
- Test: `tests/test_storage_backend.py`

- [ ] **Step 1: Write failing tests**
  Add tests for `agent_session_bucket`, `session_workspace_prefix`, `session_workspace_key`, and `LocalBackend.resolve_workspace_path`.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_storage_backend.py -q`
  Expected: FAIL because the helpers and backend method do not exist.

- [ ] **Step 3: Implement helpers and backend method**
  Add `agent_session_bucket(settings.storage.bucket)`, `session_workspace_prefix(session_id)`, and `session_workspace_key(session_id, key="")`. Add `resolve_workspace_path(bucket, session_id)` to `StorageBackend`, `LocalBackend`, and `S3Backend`.

- [ ] **Step 4: Verify tests pass**
  Run: `pytest tests/test_storage_backend.py -q`
  Expected: PASS.

### Task 2: Session Creation And Deletion

**Files:**
- Modify: `surogates/api/routes/sessions.py`
- Modify: `surogates/api/routes/prompts.py`
- Modify: `surogates/api/routes/website.py`
- Test: `tests/test_session_workspace_storage.py`

- [ ] **Step 1: Write failing tests**
  Test that web/API/website session creation stores `storage_bucket == settings.storage.bucket`, `workspace_path` from `resolve_workspace_path`, and creates only the agent bucket. Test deletion removes keys under `sessions/{session_id}/` and does not delete the bucket.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_session_workspace_storage.py -q`
  Expected: FAIL because routes still use `session-{session_id}` buckets.

- [ ] **Step 3: Implement route changes**
  Use `agent_session_bucket(settings.storage.bucket)` and `storage.resolve_workspace_path(bucket, session_id)` in all session creation paths. Delete session workspace keys with `session_workspace_prefix(session_id)` on session archive.

- [ ] **Step 4: Verify tests pass**
  Run: `pytest tests/test_session_workspace_storage.py -q`
  Expected: PASS.

### Task 3: Workspace And Artifact Key Mapping

**Files:**
- Modify: `surogates/api/routes/workspace.py`
- Modify: `surogates/api/routes/artifacts.py`
- Modify: `surogates/artifacts/store.py`
- Test: `tests/test_session_workspace_storage.py`
- Test: `tests/test_artifacts.py`

- [ ] **Step 1: Write failing tests**
  Test workspace upload/read/list/delete maps relative paths under `sessions/{session_id}/`. Test `ArtifactStore` writes `_artifacts` under the session prefix.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_session_workspace_storage.py tests/test_artifacts.py -q`
  Expected: FAIL because keys are still written at bucket root.

- [ ] **Step 3: Implement key mapping**
  Resolve session buckets without fallback. List with `prefix=session_workspace_prefix(session_id)`, strip that prefix for UI paths, and wrap all object reads/writes/deletes/stats with `session_workspace_key(session_id, path)`. Give `ArtifactStore` a required `key_prefix`.

- [ ] **Step 4: Verify tests pass**
  Run: `pytest tests/test_session_workspace_storage.py tests/test_artifacts.py -q`
  Expected: PASS.

### Task 4: Sandbox, Skill Staging, And Cleanup

**Files:**
- Modify: `surogates/harness/tool_exec.py`
- Modify: `surogates/storage/skill_staging.py`
- Modify: `surogates/api/routes/skills.py`
- Modify: `surogates/sandbox/kubernetes.py`
- Modify: `images/s3fs/entrypoint.sh`
- Modify: `surogates/jobs/cleanup_sessions.py`
- Test: `tests/test_k8s_sandbox.py`
- Test: `tests/test_skill_staging.py`
- Test: `tests/test_session_workspace_storage.py`

- [ ] **Step 1: Write failing tests**
  Test sandbox resources use `s3://{agent_bucket}/sessions/{session_id}/`, Kubernetes emits `S3_BUCKET_PATH` as `bucket:/sessions/{session_id}`, skill staging writes under the session prefix, and cleanup deletes orphaned session prefixes without deleting the agent bucket.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_k8s_sandbox.py tests/test_skill_staging.py tests/test_session_workspace_storage.py -q`
  Expected: FAIL because current code uses per-session buckets and bucket-root mounts.

- [ ] **Step 3: Implement changes**
  Build sandbox resource refs with bucket plus session path, parse bucket paths in Kubernetes, mount `S3_BUCKET_PATH` in the s3fs entrypoint, pass the agent bucket into `SkillStager`, and rewrite cleanup to scan session prefixes in the agent bucket.

- [ ] **Step 4: Verify tests pass**
  Run: `pytest tests/test_k8s_sandbox.py tests/test_skill_staging.py tests/test_session_workspace_storage.py -q`
  Expected: PASS.

### Task 5: Final Verification

**Files:**
- Modify as needed: docs and tests touched above.

- [ ] **Step 1: Run focused test suite**
  Run: `pytest tests/test_storage_backend.py tests/test_session_workspace_storage.py tests/test_artifacts.py tests/test_k8s_sandbox.py tests/test_skill_staging.py -q`
  Expected: PASS.

- [ ] **Step 2: Inspect git diff**
  Run: `git diff --stat && git diff --check`
  Expected: no whitespace errors and only intended files changed.
