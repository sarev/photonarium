import stylistic from '@stylistic/eslint-plugin';
import globals from 'globals';

export default [
    {
        files: ['app/static/**/*.js'],
        plugins: {
            '@stylistic': stylistic,
        },
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'script',
            globals: {
                ...globals.browser,
                // Project globals defined across script files
                App: 'readonly',
                AppState: 'readonly',
                RAW_EXTENSIONS: 'readonly',
                VirtualGrid: 'readonly',
                GridSelection: 'readonly',
                ThumbnailLoader: 'readonly',
                FaceThumbnails: 'readonly',
                Settings: 'readonly',
                Fullscreen: 'readonly',
                Gallery: 'readonly',
                Search: 'readonly',
                Database: 'readonly',
                Duplicates: 'readonly',
                Faces: 'readonly',
                OnThisDay: 'readonly',
            },
        },
        rules: {
            // --- Error detection ---
            'no-undef': 'error',
            'no-unused-vars': ['warn', {
                args: 'none',
                caughtErrors: 'none',
                varsIgnorePattern: '^_',
            }],
            'no-constant-condition': 'error',
            'no-dupe-args': 'error',
            'no-dupe-keys': 'error',
            'no-duplicate-case': 'error',
            'no-unreachable': 'error',
            'no-unsafe-negation': 'error',
            'use-isnan': 'error',
            'valid-typeof': 'error',
            'no-self-assign': 'error',
            'no-self-compare': 'error',
            'no-template-curly-in-string': 'warn',

            // --- Formatting via @stylistic ---
            '@stylistic/indent': ['error', 4, { SwitchCase: 1 }],
            '@stylistic/quotes': ['error', 'single', { avoidEscape: true }],
            '@stylistic/semi': ['error', 'always'],
            '@stylistic/comma-dangle': ['error', 'always-multiline'],
            '@stylistic/comma-spacing': 'error',
            '@stylistic/key-spacing': 'error',
            '@stylistic/space-before-blocks': 'error',
            '@stylistic/keyword-spacing': 'error',
            '@stylistic/space-infix-ops': 'error',
            '@stylistic/space-unary-ops': ['error', { words: true, nonwords: false }],
            '@stylistic/arrow-spacing': 'error',
            '@stylistic/no-trailing-spaces': 'error',
            '@stylistic/eol-last': ['error', 'always'],
            '@stylistic/no-multiple-empty-lines': ['error', { max: 2, maxEOF: 1 }],
            '@stylistic/brace-style': ['error', '1tbs', { allowSingleLine: true }],
            '@stylistic/no-multi-spaces': ['error', { ignoreEOLComments: true }],
        },
    },
];
