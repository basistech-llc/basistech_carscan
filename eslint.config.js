// ESLint flat config for CarScan project
// ESLint v9+ uses this new flat config format

import globals from "globals";
import js from "@eslint/js";

export default [
    {
        languageOptions: {
            ecmaVersion: "latest",
            sourceType: "module",
            globals: {
              ...globals.browser
            },
        },
        rules: {
            "no-undef": "error",
            "no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
            "no-console": "off",
        },
    },
    {
        ignores: [
            "**/assets/**",
            "**/node_modules/**",
            "**/.*",
            "**/dist/**",
            "**/build/**",
        ],
    },
];
