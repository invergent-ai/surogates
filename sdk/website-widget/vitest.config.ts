import { defineConfig } from 'vitest/config';

// ``happy-dom`` gives us a fast fetch + EventSource-like DOM environment
// for unit tests without spinning up a real browser.  Integration tests
// that need a live backend will be added under a separate
// ``tests/integration/`` directory in a later phase (Playwright).
export default defineConfig({
  test: {
    environment: 'happy-dom',
    include: ['tests/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      include: ['src/**/*.ts'],
    },
  },
});
