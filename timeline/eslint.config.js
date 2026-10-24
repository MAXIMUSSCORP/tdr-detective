// @ts-check
const js = require("@eslint/js");
const { defineConfig } = require("eslint/config");
const reactHooks = require("eslint-plugin-react-hooks");
const globals = require("globals");
const tseslint = require("typescript-eslint");

const unusedVars = ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }];

module.exports = defineConfig(
  { ignores: ["**/node_modules/", "**/dist/", "**/coverage/"] },
  {
    files: ["eslint.config.js", "api/**/*.js"],
    extends: [js.configs.recommended],
    languageOptions: { sourceType: "commonjs", globals: globals.node },
    rules: { "no-unused-vars": unusedVars },
  },
  {
    files: ["api/test/**/*.js"],
    languageOptions: { globals: globals.jest },
  },
  {
    files: ["web/**/*.{ts,tsx}"],
    extends: [js.configs.recommended, tseslint.configs.recommended, reactHooks.configs.flat.recommended],
    languageOptions: { globals: globals.browser },
    rules: { "@typescript-eslint/no-unused-vars": unusedVars },
  },
);
