import { defineConfig } from 'tsup';

// Two build targets, each with a different dependency strategy.
//
// * **npm/ESM+CJS** — AG-UI, RxJS, and friends are ``peerDependencies``
//   on the consumer's end, so we mark them ``external``.  Application
//   bundlers (Vite, webpack, Next) dedupe them against the consumer's
//   own copies and our package stays <5 KB of glue.
//
// * **IIFE (CDN)** — ``<script>`` users can't run ``npm install``, so we
//   bundle AG-UI and RxJS right into the IIFE and expose the whole kit
//   on ``window.SurogatesWidget``.  This is the "drop a tag into your
//   CMS" path; expect ~80 KB gzipped.
const externalPeers = ['@ag-ui/client', '@ag-ui/core', 'rxjs'];

export default defineConfig([
  {
    entry: { index: 'src/index.ts' },
    format: ['esm', 'cjs'],
    dts: true,
    sourcemap: true,
    clean: true,
    treeshake: true,
    target: 'es2020',
    outDir: 'dist',
    external: externalPeers,
  },
  {
    entry: { 'surogates-widget': 'src/index.ts' },
    format: ['iife'],
    globalName: 'SurogatesWidget',
    sourcemap: true,
    clean: false,
    minify: true,
    target: 'es2020',
    outDir: 'dist',
    // Deliberately no ``external`` -- AG-UI + RxJS bake into the IIFE.
  },
]);
