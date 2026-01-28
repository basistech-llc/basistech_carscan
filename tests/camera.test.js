/**
 * Minimal tests for camera.js using Node.js built-in test runner
 */

import { test } from 'node:test';
import assert from 'node:assert';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Set up jsdom environment with runScripts enabled
const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', {
  url: 'http://localhost/',
  pretendToBeVisual: true,
  runScripts: 'dangerously',
  resources: 'usable',
});

// Create DOM elements that camera.js expects
const video = dom.window.document.createElement('video');
video.id = 'video';
const canvas = dom.window.document.createElement('canvas');
canvas.id = 'canvas';
const stateSelect = dom.window.document.createElement('select');
stateSelect.id = 'state-select';
stateSelect.value = 'MA';
dom.window.document.body.appendChild(video);
dom.window.document.body.appendChild(canvas);
dom.window.document.body.appendChild(stateSelect);

// Create result display element
const resultDisplay = dom.window.document.createElement('div');
resultDisplay.id = 'result-display';
const resultName = dom.window.document.createElement('h2');
resultName.id = 'result-name';
const resultDetails = dom.window.document.createElement('p');
resultDetails.id = 'result-details';
resultDisplay.appendChild(resultName);
resultDisplay.appendChild(resultDetails);
dom.window.document.body.appendChild(resultDisplay);

// Mock browser APIs
dom.window.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
dom.window.alert = () => {};
dom.window.console = {
  ...console,
  warn: () => {},
  error: () => {},
};
dom.window.navigator.mediaDevices = {
  getUserMedia: () => Promise.reject(new Error('Mocked camera access')),
};

// Load camera.js and execute it via script tag
const cameraJsPath = join(__dirname, '../src/app/static/camera.js');
const cameraJsCode = readFileSync(cameraJsPath, 'utf-8');

// Create and execute script
const script = dom.window.document.createElement('script');
script.textContent = cameraJsCode;
dom.window.document.body.appendChild(script);

// Wait a tick for script execution
await new Promise(resolve => dom.window.setTimeout(resolve, 10));

// Access functions from window
const showResult = dom.window.showResult;

test('showResult displays success result correctly', () => {
  resultDisplay.className = 'result-box hidden';
  resultName.innerText = '';
  resultDetails.innerText = '';

  showResult('Authorized User', 'ABC1234 (MA)', true);

  assert.strictEqual(resultDisplay.classList.contains('hidden'), false);
  assert.strictEqual(resultDisplay.classList.contains('success'), true);
  assert.strictEqual(resultDisplay.classList.contains('failure'), false);
  assert.strictEqual(resultName.innerText, 'Authorized User');
  assert.strictEqual(resultDetails.innerText, 'ABC1234 (MA)');
});

test('showResult displays failure result correctly', () => {
  resultDisplay.className = 'result-box hidden';
  resultName.innerText = '';
  resultDetails.innerText = '';

  showResult('Unknown', 'XYZ9999 (VA)', false);

  assert.strictEqual(resultDisplay.classList.contains('hidden'), false);
  assert.strictEqual(resultDisplay.classList.contains('success'), false);
  assert.strictEqual(resultDisplay.classList.contains('failure'), true);
  assert.strictEqual(resultName.innerText, 'Unknown');
  assert.strictEqual(resultDetails.innerText, 'XYZ9999 (VA)');
});

test('showResult displays processing state correctly', () => {
  resultDisplay.className = 'result-box hidden';
  resultName.innerText = '';
  resultDetails.innerText = '';

  showResult('Processing...', 'Analyzing Image...', null);

  assert.strictEqual(resultDisplay.classList.contains('hidden'), false);
  assert.strictEqual(resultDisplay.classList.contains('processing'), true);
  assert.strictEqual(resultName.innerText, 'Processing...');
  assert.strictEqual(resultDetails.innerText, 'Analyzing Image...');
});
