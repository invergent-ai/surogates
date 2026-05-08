import { describe, expect, it } from "vitest";
import { exportArtifact } from "../src/components/chat/artifacts/artifact-export";
import type { ArtifactPayload } from "../src/types";

describe("artifact export", () => {
  it("exports Chart.js chart specs as JSON", () => {
    const payload: ArtifactPayload = {
      kind: "chart",
      meta: {
        artifact_id: "artifact-1",
        session_id: "session-1",
        name: "Token chart",
        kind: "chart",
        version: 1,
        size: 100,
        created_at: "2026-05-08T00:00:00Z",
      },
      spec: {
        chart_js: {
          type: "bar",
          data: { labels: ["input"], datasets: [{ data: [12] }] },
        },
      },
    };

    expect(exportArtifact(payload)).toEqual({
      text: JSON.stringify(payload.spec.chart_js, null, 2),
      mime: "application/json",
      extension: "json",
    });
  });
});
