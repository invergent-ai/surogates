export const FAKE_MODEL = "example-model";

const FENCE = "```";

/** The reply for *prompt*. The same prompt always gets the same reply. */
export function fakeReply(prompt: string): string {
  const text = prompt.trim();
  if (text.startsWith("/")) {
    const command = text.split(/\s+/)[0];
    return `Received \`${command}\`. The example backend has no model and runs no commands, so this is a scripted reply.`;
  }
  return [
    `You said: "${text}"`,
    "",
    "This reply is scripted by the example backend; no language model is behind it. It gives the chat something to render:",
    "",
    "- **Streaming:** the text arrives in small chunks, as `llm.delta` events.",
    "- **Markdown:** lists, `inline code`, tables and code blocks draw as below.",
    "",
    "| Event | Meaning |",
    "| --- | --- |",
    "| `llm.delta` | a chunk of the reply |",
    "| `llm.response` | the finished reply |",
    "",
    `${FENCE}ts`,
    "export function hello(name: string) {",
    "  return `Hello, ${name}`;",
    "}",
    FENCE,
    "",
    "Try `/demo-tools`, `/demo-artifacts`, `/demo-ask-user-question`, `/demo-expert` or `/demo-errors` for the rest.",
  ].join("\n");
}

/** *reply* in word-sized chunks, *delayMs* apart; joined, they are *reply* again. */
export async function* streamReply(
  reply: string,
  delayMs = 12,
): AsyncGenerator<string> {
  for (const chunk of reply.match(/\s*\S+\s*/g) ?? []) {
    if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
    yield chunk;
  }
}

/** A rough token count: one per four characters. */
export function countTokens(text: string) {
  return Math.ceil(text.length / 4);
}
