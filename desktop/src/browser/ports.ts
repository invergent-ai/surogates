// A chat's own servers, as its agent's browser names them (spec, Section 5): a port of this
// computer's loopback, by one of its three names, over plain http. The browser's one private
// destination: the port its chat's user allowed is carried into that chat's sandbox, never to
// this computer's own loopback. Every other spelling of this computer stays refused.

import { HTTP_PORT, SOCKS_PORT } from "../guest/listeners.js";

// The names a chat's own server is opened by, as a URL spells them: decimal, octal, hex and short
// forms of 127.0.0.1 are that address. Not a name under localhost, 0.0.0.0, another address of
// 127.0.0.0/8, an IPv4 address inside an IPv6 one, or a name that only resolves here.
export const CHAT_HOSTS: ReadonlySet<string> = new Set(["localhost", "127.0.0.1", "[::1]"]);
// The sandbox's own proxies for its chat's commands (guest/listeners.ts): never the browser's to open.
export const SANDBOX_PORTS: ReadonlySet<number> = new Set([HTTP_PORT, SOCKS_PORT]);

/** The port of a chat's own servers that *host*:*port* names, an IPv6 address in brackets; null for any other destination. */
export function chatPort(host: string, port: number): number | null {
  if (!Number.isInteger(port) || port < 1 || port > 65_535 || !/^[A-Za-z0-9.\-[\]:]+$/.test(host)) return null;
  let name: string;
  try {
    name = new URL(`http://${host}/`).hostname.replace(/\.$/, "");
  } catch {
    return null;
  }
  return CHAT_HOSTS.has(name) ? port : null;
}

/** The port of a chat's own servers that the address *url* opens, over plain http; null for any other address. */
export function chatPortOf(url: string): number | null {
  let address: URL;
  try {
    address = new URL(url);
  } catch {
    return null;
  }
  if (address.protocol !== "http:" || address.username !== "" || address.password !== "") return null;
  return chatPort(address.hostname, Number(address.port || 80));
}
