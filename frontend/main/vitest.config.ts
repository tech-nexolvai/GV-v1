import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config.ts'

// Interactive component tests (clicks, open/close, theme) for the new shadcn/ui code, beside the
// existing node harness (`npm run test:components`). Both run in CI: `npm test`.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      include: ['tests/vitest/**/*.test.{ts,tsx}'],
      environment: 'node', // a file that needs a DOM opts in with `// @vitest-environment jsdom`
      setupFiles: ['tests/vitest/setup.ts'],
      restoreMocks: true,
    },
  }),
)
