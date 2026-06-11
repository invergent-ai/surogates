# Image & Video Generation Support — Design

**Date**: 2026-06-11
**Status**: Approved
**Scope**: surogates framework (`/work/surogates`)

## Goal

Give agents the ability to generate images and videos via OpenRouter-hosted
models, end-to-end: configuration, endpoint resolution, and two new builtin
tools (`generate_image`, `generate_video`) that write results into the
session workspace so they can be delivered to users through the existing
media outbox (`media_type` / `media_path` payload fields).

## Provider API shapes (OpenRouter)

- **Image generation** — synchronous, via `POST /chat/completions` with
  `modalities: ["image", "text"]`. The generated image returns as a
  base64-encoded data URL in `message.images[].image_url.url`. Universal
  params: `aspect_ratio` (1:1 … 21:9), `image_size` (`0.5K`, `1K` default,
  `2K`, `4K`). Image-to-image works by including input images as
  `image_url` content parts in the user message.
- **Video generation** — asynchronous job API: `POST /videos` returns a job
  ID (202), poll `GET /videos/{jobId}` until `completed` / `failed`, then
  download from `unsigned_urls[0]`. Unified params: `duration`,
  `resolution` (480p–4K), `aspect_ratio`, `frame_images` (image-to-video),
  `generate_audio`, `seed`. Renders take minutes.

## 1. Config — `LLMSettings` additions (`surogates/config.py`)

Follows the existing `summary_*` / `vision_*` / `advisor_*` triple pattern:

```python
image_model: str = ""      # e.g. "google/gemini-2.5-flash-image" (empty = tool disabled)
image_base_url: str = ""   # falls back to main llm base_url
image_api_key: str = ""    # falls back to main llm api_key

video_model: str = ""      # e.g. "google/veo-3.1" (empty = tool disabled)
video_base_url: str = ""   # falls back to main llm base_url (endpoint must support /videos, i.e. OpenRouter)
video_api_key: str = ""    # falls back to main llm api_key
video_timeout: int = 600       # max seconds to wait for a render
video_poll_interval: int = 10  # seconds between job polls
```

YAML/env injection (`SUROGATES_LLM_IMAGE_MODEL`, `SUROGATES_LLM_VIDEO_MODEL`,
etc.) comes free from the existing `_flatten_yaml` + `env_prefix="SUROGATES_LLM_"`
mechanism.

**An empty model string disables the corresponding tool entirely** — the
tool is not registered. There is no fallback to the main chat model: the
main model generally cannot generate media, and silently degrading would
be misleading.

## 2. Endpoint resolution

- Add `llm_image` and `llm_video` `LLMEndpoint` slots to
  `AgentRuntimeContext` (`surogates/runtime/context.py`), mirroring
  `llm_vision`.
- Tools resolve their endpoint the same way `vision_analyze` does today:
  context slot first, then `settings.llm.image_*` / `video_*`, with the
  main `base_url` / `api_key` filling any missing pieces.
- Populating these slots from surogate-ops runtime-config is **out of
  scope** for this pass; unset slots simply mean global settings apply.

## 3. `generate_image` tool

New builtin module `surogates/tools/builtin/media_gen.py`.

- **Transport**: `AsyncOpenAI` chat-completions call with
  `extra_body={"modalities": ["image", "text"]}` (consistent with the
  framework's AsyncOpenAI usage everywhere else).
- **Parameters**:
  - `prompt` (required) — text description of the image
  - `aspect_ratio` (optional) — e.g. `1:1`, `16:9`, `9:16`
  - `image_size` (optional, default `1K`) — `0.5K` / `1K` / `2K` / `4K`
  - `input_images` (optional) — workspace-relative paths for
    image-to-image; sent as `image_url` content parts (data URLs)
  - `output_path` (optional) — workspace-relative output path; default
    `media/images/<slug>.png`
- **Behavior**: decode the base64 data URL from
  `message.images[0].image_url.url`, write atomically into the workspace
  (containment-validated, same pattern as `file_ops`), return the relative
  path plus any accompanying assistant text. The returned path is directly
  usable as `media_path` in a delivery-outbox payload.

## 4. `generate_video` tool

Same module.

- **Transport**: plain `httpx` against the resolved video endpoint —
  `POST {base_url}/videos`, poll `GET {base_url}/videos/{id}` every
  `video_poll_interval` seconds, on `completed` download
  `unsigned_urls[0]`. Blocking inside the tool call, capped at
  `video_timeout` (long tool calls are already normal in the harness —
  terminal default is 300 s).
- **Parameters**:
  - `prompt` (required)
  - `duration` (optional, seconds)
  - `resolution` (optional) — `480p` / `720p` / `1080p` / `1K` / `2K` / `4K`
  - `aspect_ratio` (optional)
  - `first_frame_image` (optional) — workspace-relative path, sent via
    `frame_images` with role `first_frame` (image-to-video)
  - `output_path` (optional) — default `media/videos/<slug>.mp4`
- **Behavior**: write the video into the workspace, return the relative
  path, plus the reported generation cost from `usage` when present.
- **Timeout**: on `video_timeout` expiry the tool returns an error string
  that includes the job ID and polling URL, so the agent can tell the
  user the render is still in progress rather than silently failing.

## 5. Registration

- `media_gen.register(registry)` is added to the builtin module list in
  `surogates/tools/runtime.py`.
- Each tool registers only when its model is configured (empty-string
  disable, same gating idea as the KB tools' empty-URL disable).
- Toolset name: `media_gen`.

## 6. Error handling

- Provider API errors and `failed` job statuses return descriptive
  tool-error strings (standard tool convention; the model sees the error
  and can react).
- Workspace writes are atomic (`.tmp` + `os.replace`) and
  containment-validated against the session workspace root.

## 7. Testing

Unit tests with mocked `AsyncOpenAI` / `httpx`:

- config defaults and `SUROGATES_LLM_IMAGE_*` / `VIDEO_*` env overrides
- endpoint fallback chain (dedicated → main base_url/api_key)
- image: data-URL decode, workspace write, containment rejection,
  image-to-image content parts
- video: poll loop `pending → in_progress → completed`, `failed` status,
  timeout path (job ID surfaced), download + write
- registration gating: tools absent when models unset

## Out of scope

- surogate-ops UI / runtime-config plumbing for per-agent image/video
  endpoints
- auto-sending generated media to channels (the agent decides, using the
  existing outbox mechanism)
- provider-specific passthrough params (Recraft styles, Sourceful fonts,
  `provider` object for video)
- a non-blocking job-style `generate_video` (submit + status tool) — can
  be added later if blocking proves limiting