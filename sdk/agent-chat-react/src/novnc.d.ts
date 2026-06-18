/// <reference types="novnc__novnc" />
// @novnc/novnc@1.7 exposes the RFB class at the bare specifier (its
// package.json "exports" maps the package root to "./core/rfb.js"), while
// @types/novnc__novnc still declares it at the legacy "@novnc/novnc/lib/rfb"
// path. Bridge the runtime import path to the published typings.
declare module "@novnc/novnc" {
  export { default } from "@novnc/novnc/lib/rfb";
}
