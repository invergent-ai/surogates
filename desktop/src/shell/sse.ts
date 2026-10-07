// Server-sent events, read off a response's body as the agent sends them (sse-starlette):
// each event's id, type and data, one event to each blank line. A comment (": ping") keeps
// the stream alive and is no event.

export interface ServerEvent {
  id: string | null; // the last id the stream named
  type: string;
  data: string;
}

/** Read *body* to its end: *onEvent* hears each event, and *heard* each chunk, a comment's included. */
export async function readEvents(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: ServerEvent) => void,
  heard: () => void = () => {},
): Promise<void> {
  const decoder = new TextDecoder();
  let pending = "";
  let id: string | null = null;
  let type = "message";
  let data: string[] = [];
  const line = (text: string): void => {
    if (text === "") {
      if (data.length > 0) onEvent({ id, type, data: data.join("\n") });
      type = "message";
      data = [];
      return;
    }
    if (text.startsWith(":")) return;
    const colon = text.indexOf(":");
    const field = colon < 0 ? text : text.slice(0, colon);
    const value = colon < 0 ? "" : text.slice(colon + 1).replace(/^ /, "");
    if (field === "id") id = value;
    else if (field === "event") type = value;
    else if (field === "data") data.push(value);
  };
  const reader = body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    heard();
    pending += decoder.decode(value, { stream: true });
    // ponytail: lines end in \n or \r\n, as the agent ends them; a lone \r, which the format also allows, would need a held-back \r across chunks.
    const lines = pending.split("\n");
    pending = lines.pop() ?? "";
    for (const each of lines) line(each.endsWith("\r") ? each.slice(0, -1) : each);
  }
}
