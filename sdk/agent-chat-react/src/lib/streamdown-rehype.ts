// Streamdown's own rehype plugins, its link hardening included, with one change: a link it refuses
// (a relative address such as thread/<id>, which the model can write and no chat opens) shows its
// words alone, not its words and a " [blocked]" marker.

import remarkBreaks from "remark-breaks";
import { defaultRehypePlugins, defaultRemarkPlugins, type StreamdownProps } from "streamdown";

type Plugins = NonNullable<StreamdownProps["rehypePlugins"]>;

export const streamdownRehypePlugins: Plugins = Object.entries(defaultRehypePlugins).map(([name, plugin]) => {
  if (name !== "harden" || !Array.isArray(plugin)) return plugin;
  const [harden, options] = plugin;
  return [harden, { ...(options as object), linkBlockPolicy: "text-only" }] as Plugins[number];
});

// Streamdown's own remark plugins, plus one: a single newline is a line break, as people and
// models mean it in chat, not a space that joins two lines into one.
export const streamdownRemarkPlugins: NonNullable<StreamdownProps["remarkPlugins"]> = [
  ...Object.values(defaultRemarkPlugins),
  remarkBreaks,
];
