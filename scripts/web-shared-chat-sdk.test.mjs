import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../", import.meta.url));

function repoPath(path) {
  return new URL(path, `file://${repoRoot}`).pathname;
}

const forbiddenLocalSharedUi = [
  "web/src/components/chat",
  "web/src/components/workspace-panel.tsx",
  "web/src/components/file-viewer.tsx",
  "web/src/hooks/use-session-runtime.ts",
];

for (const path of forbiddenLocalSharedUi) {
  assert.equal(
    existsSync(repoPath(path)),
    false,
    `${path} should not exist; shared chat UI must come from @invergent/agent-chat-react`,
  );
}

for (const path of [
  "web/src/features/chat/chat-page.tsx",
  "web/src/features/skills/skills-page.tsx",
  "web/src/features/agents/agents-page.tsx",
]) {
  const source = readFileSync(repoPath(path), "utf8");
  assert.match(
    source,
    /@invergent\/agent-chat-react/,
    `${path} should import shared chat primitives from @invergent/agent-chat-react`,
  );
  assert.doesNotMatch(
    source,
    /@\/components\/(?:chat|workspace-panel|file-viewer|ai-elements\/message)/,
    `${path} should not import duplicated shared chat UI from web/src/components`,
  );
}
