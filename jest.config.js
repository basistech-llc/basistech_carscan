export default {
  testEnvironment: 'jsdom',
  testMatch: ['**/tests/**/*.test.js'],
  collectCoverageFrom: [
    'src/app/static/**/*.js',
    '!src/app/static/assets/**',
  ],
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov'],
};
