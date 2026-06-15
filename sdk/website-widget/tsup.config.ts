import { defineConfig } from 'tsup';

// Three build targets, each with a different dependency strategy.
//
// * **npm/ESM+CJS (headless)** — ``index`` exports only the AG-UI
//   ``WebsiteAgent``.  AG-UI, RxJS, and friends are ``peerDependencies``
//   on the consumer's end, so we mark them ``external``; application
//   bundlers dedupe them and the headless package stays ~6 KB of glue.
//   Preact is *not* imported by this entry, so it never lands here.
//
// * **npm/ESM+CJS (``ui`` subpath)** — the self-mounting widget.  Preact
//   is bundled (not external) so bundler consumers don't have to install
//   it; AG-UI stays external like the headless entry.
//
// * **IIFE (CDN)** — ``<script>`` users can't run ``npm install``, so we
//   bundle everything (AG-UI + RxJS + Preact + the UI) and expose
//   ``WebsiteAgent`` *and* ``mount`` on ``window.SurogatesWidget``.
const externalPeers = ['@ag-ui/client', '@ag-ui/core', 'rxjs'];

export default defineConfig([
  {
    entry: { index: 'src/index.ts', ui: 'src/ui/mount.tsx' },
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
    entry: { 'surogates-widget': 'src/global.ts' },
    format: ['iife'],
    globalName: 'SurogatesWidget',
    sourcemap: true,
    clean: false,
    minify: true,
    target: 'es2020',
    outDir: 'dist',
    // ``platform: 'browser'`` is load-bearing: transitive deps (uuid,
    // nanoid via AG-UI) ship CJS variants that ``require('crypto')`` at
    // module-eval time.  Under the default node platform esbuild bundles
    // those, and the IIFE throws "Dynamic require of crypto" on load in
    // a real browser.  Browser resolution picks their Web-Crypto builds.
    platform: 'browser',
    // Deliberately no ``external`` -- AG-UI + RxJS + Preact bake in.
  },
]);
