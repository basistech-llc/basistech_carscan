// ESLint flat config for CarScan project
// ESLint v9+ uses this new flat config format

export default [
    {
        languageOptions: {
            ecmaVersion: "latest",
            sourceType: "module",
            globals: {
                // Browser globals
                window: "readonly",
                document: "readonly",
                console: "readonly",
                fetch: "readonly",
                navigator: "readonly",
                alert: "readonly",
                setTimeout: "readonly",
                setInterval: "readonly",
                clearInterval: "readonly",
                FormData: "readonly",
                AbortController: "readonly",
                Blob: "readonly",
                Image: "readonly",
                URL: "readonly",
                URLSearchParams: "readonly",
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
