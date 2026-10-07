// render.mjs: turn demo.html into vibey-demo.mp4, one deterministic frame at a time.
//
//   node render.mjs [out.mp4] [fps]
//
// Needs Playwright (Chromium) and ffmpeg. Drives window.render(t) for every frame,
// pipes JPEG screenshots into ffmpeg, and muxes in the synthesized soundtrack.
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import os from 'node:os';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')); }

const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.resolve(process.argv[2] || path.join(here, 'vibey-demo.mp4'));
const FPS = +(process.argv[3] || 30);
const wav = path.join(os.tmpdir(), 'vibey-demo-soundtrack.wav');
execFileSync('python3', [path.join(here, 'soundtrack.py'), wav], { stdio: 'inherit' });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
page.on('pageerror', (e) => console.error('page error:', e.message));
await page.goto('file://' + path.join(here, 'demo.html') + '?capture');
await page.waitForLoadState('networkidle');
await page.evaluate(() => document.fonts.ready);
const DURATION = await page.evaluate(() => window.DURATION);
const frames = Math.round(DURATION * FPS);

const ff = spawn('ffmpeg', [
  '-y', '-hide_banner', '-loglevel', 'error',
  '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg', '-i', '-',
  '-i', wav,
  '-c:v', 'libx264', '-preset', 'slow', '-crf', '24', '-pix_fmt', 'yuv420p', '-profile:v', 'high',
  '-c:a', 'aac', '-b:a', '128k', '-shortest', '-movflags', '+faststart', out,
], { stdio: ['pipe', 'inherit', 'inherit'] });

for (let i = 0; i < frames; i++) {
  await page.evaluate((t) => window.render(t), i / FPS);
  const buf = await page.screenshot({ type: 'jpeg', quality: 92 });
  if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r));
  if (i % FPS === 0) process.stdout.write(`\r${(i / FPS).toFixed(0)}s / ${DURATION}s`);
}
ff.stdin.end();
await new Promise((r) => ff.on('close', r));
await browser.close();
console.log(`\nwrote ${out}`);
