import express, {
  type ErrorRequestHandler,
  type Request,
  type Response,
} from "express";
import multer from "multer";
import {
  DEMO_SLASH_COMMANDS,
  runDemoCommand,
  submitClarifyDemo,
  submitExpertFeedbackDemo,
} from "./demo-commands";
import { resolveOpenAiConfig, streamChatCompletions } from "./openai";
import {
  ExampleSessionStore,
  normalizePath,
  type ExampleSessionRecord,
} from "./session-store";
import { writeSseEvent } from "./events";

export interface CreateExampleAppInput {
  store?: ExampleSessionStore;
  fetchImpl?: typeof fetch;
}

const upload = multer({ storage: multer.memoryStorage() });

export function createExampleApp({
  store = new ExampleSessionStore(),
  fetchImpl = fetch,
}: CreateExampleAppInput = {}) {
  const app = express();
  app.locals.store = store;
  app.use(express.json({ limit: "1mb" }));

  app.get("/api/config", (_request, response) => {
    const config = resolveOpenAiConfig();
    response.json({
      model: config.model,
      baseUrl: config.baseUrl,
      hasApiKey: Boolean(config.apiKey),
    });
  });

  app.get("/api/sessions", (request, response) => {
    response.json(store.list(numberQuery(request, "limit", 50), numberQuery(request, "offset", 0)));
  });

  app.post("/api/sessions", (request, response) => {
    response.status(201).json(
      store.create({
        agentId: stringBody(request, "agentId"),
        system: stringBody(request, "system"),
      }).session,
    );
  });

  app.get("/api/sessions/:sessionId", (request, response) => {
    response.json(requireRecord(store, request).session);
  });

  app.delete("/api/sessions/:sessionId", (request, response) => {
    store.delete(request.params.sessionId);
    response.status(204).end();
  });

  app.post("/api/sessions/:sessionId/messages", async (request, response, next) => {
    const record = requireRecord(store, request);
    const content = stringBody(request, "content");
    if (!content) {
      response.status(400).json({ error: "content is required" });
      return;
    }
    const userEvent = record.events.append("user.message", { content });
    record.history.push({ role: "user", content });
    record.lastUserMessage = content;
    record.session.messageCount = (record.session.messageCount ?? 0) + 1;
    store.setStatus(record, "active");
    if (runDemoCommand(store, record, content)) {
      response.json({ eventId: userEvent.eventId, status: "accepted" });
      return;
    }
    response.json({ eventId: userEvent.eventId, status: "accepted" });
    await runRealLlmTurn(record, fetchImpl, next);
  });

  app.post("/api/sessions/:sessionId/pause", (request, response) => {
    const record = requireRecord(store, request);
    store.setStatus(record, "paused");
    record.events.append("session.pause", {});
    record.events.append("session.done", {});
    response.status(204).end();
  });

  app.post("/api/sessions/:sessionId/retry", async (request, response, next) => {
    const record = requireRecord(store, request);
    if (!record.lastUserMessage) {
      response.json(record.session);
      return;
    }
    store.setStatus(record, "active");
    record.events.append("session.resume", {});
    response.json(record.session);
    await runRealLlmTurn(record, fetchImpl, next);
  });

  app.get("/api/sessions/:sessionId/events", (request, response) => {
    const record = requireRecord(store, request);
    const after = numberQuery(request, "after", 0);
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    for (const event of record.events.replay(after)) writeSseEvent(response, event);
    const unsubscribe = record.events.subscribe((event) => writeSseEvent(response, event));
    request.on("close", unsubscribe);
  });

  app.get("/api/sessions/:sessionId/artifacts/:artifactId", (request, response) => {
    response.json(store.getArtifact(requireRecord(store, request), request.params.artifactId));
  });

  app.post("/api/sessions/:sessionId/clarify/:toolCallId", (request, response) => {
    const record = requireRecord(store, request);
    submitClarifyDemo(record, request.params.toolCallId, arrayBody(request, "responses"));
    response.json({ eventId: record.events.replay().at(-1)?.eventId });
  });

  app.post("/api/sessions/:sessionId/expert-feedback", (request, response) => {
    const record = requireRecord(store, request);
    submitExpertFeedbackDemo(record, {
      targetEventId: Number(request.body?.expertResultEventId ?? 0),
      rating: request.body?.rating === "down" ? "down" : "up",
      reason: typeof request.body?.reason === "string" ? request.body.reason : undefined,
    });
    response.json({ eventId: record.events.replay().at(-1)?.eventId });
  });

  app.get("/api/slash-commands", (_request, response) => {
    response.json(DEMO_SLASH_COMMANDS);
  });

  app.get("/api/sessions/:sessionId/workspace/tree", (request, response) => {
    response.json(store.getWorkspaceTree(requireRecord(store, request)));
  });

  app.get("/api/sessions/:sessionId/workspace/file", (request, response) => {
    response.json(store.getWorkspaceFile(requireRecord(store, request), String(request.query.path ?? "")));
  });

  app.post(
    "/api/sessions/:sessionId/workspace/upload",
    upload.single("file"),
    (request, response) => {
      const record = requireRecord(store, request);
      if (!request.file) {
        response.status(400).json({ error: "file is required" });
        return;
      }
      const directory = typeof request.body?.directory === "string" ? request.body.directory : "";
      response.status(201).json(
        store.uploadWorkspaceFile(record, {
          path: normalizePath(`${directory}/${request.file.originalname}`),
          content: request.file.buffer.toString("utf8"),
          mimeType: request.file.mimetype,
        }),
      );
    },
  );

  app.delete("/api/sessions/:sessionId/workspace/file", (request, response) => {
    store.deleteWorkspaceFile(requireRecord(store, request), String(request.query.path ?? ""));
    response.status(204).end();
  });

  app.use(errorHandler);
  return app;
}

async function runRealLlmTurn(
  record: ExampleSessionRecord,
  fetchImpl: typeof fetch,
  next: (error: unknown) => void,
) {
  record.events.append("harness.wake", {});
  record.events.append("llm.request", { model: record.session.model });
  let content = "";
  let inputTokens = 0;
  let outputTokens = 0;
  try {
    for await (const chunk of streamChatCompletions({
      config: resolveOpenAiConfig(),
      messages: record.history,
      fetchImpl,
    })) {
      if (chunk.type === "delta") {
        content += chunk.content;
        record.events.append("llm.delta", { content: chunk.content });
      } else if (chunk.type === "usage") {
        record.session.model = chunk.model || record.session.model;
        inputTokens = chunk.inputTokens;
        outputTokens = chunk.outputTokens;
      }
    }
    record.history.push({ role: "assistant", content });
    record.session.inputTokens = (record.session.inputTokens ?? 0) + inputTokens;
    record.session.outputTokens = (record.session.outputTokens ?? 0) + outputTokens;
    record.events.append("llm.response", {
      message: { content },
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      model: record.session.model,
    });
    record.events.append("session.done", {});
  } catch (error) {
    record.events.append("session.fail", {
      error_category: "provider_error",
      error_title: "Provider request failed",
      error_detail: error instanceof Error ? error.message : "Unknown provider error",
      retryable: true,
    });
    record.events.append("session.done", {});
    next(error);
  }
}

function requireRecord(store: ExampleSessionStore, request: Request) {
  return store.require(String(request.params.sessionId));
}

const errorHandler: ErrorRequestHandler = (error, _request, response, _next) => {
  if (response.headersSent) return;
  const status = typeof error?.status === "number" ? error.status : 500;
  response.status(status).json({
    error: error instanceof Error ? error.message : "Internal server error",
  });
};

function numberQuery(request: Request, name: string, fallback: number) {
  const parsed = Number(request.query[name]);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function stringBody(request: Request, name: string) {
  return typeof request.body?.[name] === "string" ? request.body[name] : undefined;
}

function arrayBody(request: Request, name: string) {
  return Array.isArray(request.body?.[name]) ? request.body[name] : [];
}
