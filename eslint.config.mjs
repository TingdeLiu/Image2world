import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// eslint-config-next ships legacy eslintrc-style configs, so bridge them into
// ESLint 9 flat config with FlatCompat (the canonical Next.js 15 setup).
const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    // backend/ is a standalone Python inference service (plus a plain-Node smoke
    // test); it is not part of the Next.js app and should not be linted here.
    ignores: [
      ".next/**",
      "**/.pytest_cache/**",
      "**/.pytest-*/**",
      "out/**",
      "build/**",
      "next-env.d.ts",
      "backend/**",
    ],
  },
  {
    // React Three Fiber's JSX intrinsics (<color>, <ambientLight>, …) clash with
    // React 19's stricter JSX typings — the same friction handled by
    // `typescript.ignoreBuildErrors` in next.config.ts. Allow `@ts-nocheck` in the
    // R3F view layer so lint stays consistent with that deliberate decision.
    files: ["src/components/WorldViewer.tsx"],
    rules: {
      "@typescript-eslint/ban-ts-comment": [
        "error",
        { "ts-nocheck": false, "ts-expect-error": "allow-with-description" },
      ],
    },
  },
];

export default eslintConfig;
