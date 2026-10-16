import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

// Claude Slides' icon names, drawn from lucide-static's set: Claude Slides' own icon font stays its own.
const FILES: Record<string, string> = {
  Activity: "activity", Book: "book", Chart: "chart-column", Chat: "message-circle", Check: "check", CheckCircle: "circle-check",
  Clock: "clock", Cloud: "cloud", Code: "code", Database: "database", Globe: "globe", GraduationCap: "graduation-cap",
  Home: "house", Key: "key", Lightbulb: "lightbulb", Lightning: "zap", Link: "link", Lock: "lock", PaperPlane: "send",
  Play: "play", Search: "search", Settings: "settings", Star: "star", ThumbsUp: "thumbs-up", Tool: "hammer",
  Trust: "shield-check", Users: "users", Verified: "badge-check", Warning: "triangle-alert", Wrench: "wrench",
};
export const ICON_NAMES = Object.keys(FILES);

const require = createRequire(import.meta.url);
let nonce = 0;

/** The icon *name* in *color*, as an SVG picture *px* square drawn at twice that for its PNG fallback, and made
 * unique: of two identical SVG pictures in one command the editor keeps the SVG of the first only. */
export function iconSvg(name: string, color: string, px: number): string {
  const file = FILES[name];
  if (!file) throw new Error(`no icon ${name}`);
  const svg = readFileSync(require.resolve(`lucide-static/icons/${file}.svg`), "utf8")
    .replace(/currentColor/g, color)
    .replace(/\swidth="[^"]*"/, ` width="${px * 2}"`)
    .replace(/\sheight="[^"]*"/, ` height="${px * 2}"`);
  return `data:image/svg+xml;base64,${Buffer.from(`${svg}<!-- ${++nonce} -->`).toString("base64")}`;
}
