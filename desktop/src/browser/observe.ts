// The browser.observe scripts (surogates/devices/browser.py): reads of a page the app
// ships, run by id with the server's parameters as data. The server cannot name any other
// JavaScript an observation.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { CDPSession, Page } from "playwright-core";

// The same from src/browser and from dist/browser.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
// The snapshot's page half, the one the cloud's browser runs (surogates/browser/observe/snapshot.js),
// copied beside the build by npm run build; read at the first snapshot, so an unbuilt tree still loads this.
let collector: string | undefined;
const snapshotCollector = (): string => (collector ??= readFileSync(join(PACKAGE, "dist", "browser", "observe", "snapshot.js"), "utf8").trim());

const MAX_SELECTOR = 1_000;

type Params = Record<string, unknown>;

interface Collected {
  viewport: { width: number; height: number };
  nodes: Array<Record<string, unknown> & { idx?: number; backend_node_id?: number | null }>;
}

function selectorOf(params: Params): string | null {
  const { selector } = params;
  if (selector === null || selector === undefined) return null;
  if (typeof selector !== "string" || selector.length > MAX_SELECTOR) throw new Error("A selector is a string of at most 1000 characters");
  return selector;
}

/**
 * snapshot@1: every frame's nodes as the collector finds them, each frame's origin in the
 * page's coordinates, and each node's backend id; as the cloud's snapshot script gives them.
 */
async function snapshot(page: Page, params: Params): Promise<unknown> {
  const selector = selectorOf(params);
  const main = page.mainFrame();
  const targets = selector === null ? page.frames() : [main];
  const frames: Array<{ x: number; y: number; nodes: Collected["nodes"] }> = [];
  let viewport: Collected["viewport"] | null = null;
  let base = 0;
  for (const frame of targets) {
    let x = 0;
    let y = 0;
    if (frame !== main) {
      // Detached, hidden or zero-size: nothing to click inside.
      const box = await frame.frameElement().then((element) => element.boundingBox()).catch(() => null);
      if (!box) continue;
      x = Math.round(box.x);
      y = Math.round(box.y);
    }
    let collected: Collected;
    try {
      // The collector with its argument as a JSON literal: data, never code.
      collected = await frame.evaluate(`(${snapshotCollector()})(${JSON.stringify({ selector: frame === main ? selector : null, base })})`);
    } catch {
      continue;
    }
    if (frame === main) viewport = collected.viewport;
    base += collected.nodes.length;
    frames.push({ x, y, nodes: collected.nodes });
  }
  const cdp = await page.context().newCDPSession(page);
  try {
    const { root } = await cdp.send("DOM.getDocument", { depth: -1, pierce: true });
    const ids = new Map<string, number>();
    const walk = (node: { attributes?: string[]; backendNodeId: number; children?: unknown[]; contentDocument?: unknown } | undefined): void => {
      if (!node) return;
      const attributes = node.attributes ?? [];
      for (let at = 0; at < attributes.length; at += 2) {
        if (attributes[at] === "data-sg-i") ids.set(attributes[at + 1] ?? "", node.backendNodeId);
      }
      for (const child of node.children ?? []) walk(child as typeof node);
      walk(node.contentDocument as typeof node);
    };
    walk(root);
    for (const frame of frames) {
      for (const node of frame.nodes) node.backend_node_id = node.idx === undefined ? null : (ids.get(String(node.idx)) ?? null);
    }
  } finally {
    await cdp.detach().catch(() => {});
  }
  return { url: page.url(), title: await page.title(), viewport: page.viewportSize() ?? viewport ?? { width: 0, height: 0 }, frames };
}

// In the element's own frame: scrolled into view, then whether it can be clicked at its centre.
function probe(this: Element): { ok: boolean; cover?: string | null } {
  this.scrollIntoView({ block: "center", inline: "center" });
  const r = this.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return { ok: false };
  const hit = document.elementFromPoint(Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2));
  let cover: string | null = null;
  if (hit && hit !== this && !this.contains(hit) && !hit.contains(this)) {
    const cls = typeof hit.className === "string" ? (hit.className.split(/\s+/)[0] ?? "") : "";
    cover = hit.tagName.toLowerCase() + (hit.id ? `#${hit.id}` : "") + (cls ? `.${cls}` : "");
  }
  return { ok: true, cover };
}

async function objectOf(cdp: CDPSession, backendNodeId: number): Promise<string | null> {
  try {
    return (await cdp.send("DOM.resolveNode", { backendNodeId })).object.objectId ?? null;
  } catch {
    return null;
  }
}

/**
 * locate@1: where a ref from the last snapshot is now, as the cloud's ref click finds it: by
 * its backend id, else by role, name and nth in the accessibility tree; its centre in the page's
 * coordinates once scrolled into view, or why it cannot be clicked.
 */
async function locate(page: Page, params: Params): Promise<unknown> {
  const { backend_node_id: backend, role, name, nth } = params;
  if (typeof role !== "string" || typeof name !== "string" || typeof nth !== "number" || !Number.isInteger(nth) || nth < 0) {
    throw new Error("locate@1 takes a role, a name and an nth");
  }
  const cdp = await page.context().newCDPSession(page);
  try {
    let objectId = typeof backend === "number" && Number.isInteger(backend) ? await objectOf(cdp, backend) : null;
    if (!objectId) {
      const { nodes } = await cdp.send("Accessibility.getFullAXTree");
      const matches = nodes
        .filter((node) => !node.ignored && node.role?.value === role && node.backendDOMNodeId !== undefined)
        .filter((node) => String(node.name?.value ?? "").replace(/\s+/g, " ").trim().slice(0, 240) === name)
        .map((node) => node.backendDOMNodeId as number);
      const pick = matches[nth] ?? matches[0];
      if (pick !== undefined) objectId = await objectOf(cdp, pick);
    }
    if (!objectId) return { missing: "gone" };
    const probed = await cdp.send("Runtime.callFunctionOn", { objectId, returnByValue: true, functionDeclaration: probe.toString() });
    const found = probed.result.value as { ok?: boolean; cover?: string | null } | undefined;
    if (!found?.ok) return { missing: "hidden" };
    if (found.cover) return { covered: found.cover };
    // In the page's coordinates, a node in a frame included: the mouse speaks only those.
    const quad = await cdp.send("DOM.getBoxModel", { objectId }).then((box) => box.model.content).catch(() => null);
    if (!quad || quad.length < 8) return { missing: "unmeasurable" };
    const [x1 = 0, y1 = 0, x2 = 0, y2 = 0, x3 = 0, y3 = 0, x4 = 0, y4 = 0] = quad;
    return { x: Math.round((x1 + x2 + x3 + x4) / 4), y: Math.round((y1 + y2 + y3 + y4) / 4) };
  } finally {
    await cdp.detach().catch(() => {});
  }
}

const SCRIPTS: Record<string, (page: Page, params: Params) => Promise<unknown>> = {
  "snapshot@1": snapshot,
  "locate@1": locate,
};

export function observe(page: Page, args: Params): Promise<unknown> {
  const script = typeof args.script === "string" && Object.hasOwn(SCRIPTS, args.script) ? SCRIPTS[args.script] : undefined;
  if (!script) throw new Error(`This computer's browser has no script ${JSON.stringify(String(args.script))}`);
  const params = args.params;
  return script(page, typeof params === "object" && params !== null && !Array.isArray(params) ? (params as Params) : {});
}
