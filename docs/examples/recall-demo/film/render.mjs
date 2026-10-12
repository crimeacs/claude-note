import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
const [,, html, out, fps = '30', from = '0', to = '', stills = ''] = process.argv;
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
page.on('pageerror', e => { console.error('PAGEERROR', e.message); process.exit(2); });
await page.goto('file://' + html);
await page.evaluate(() => window.ready);
const total = await page.evaluate(() => window.TOTAL);
if (stills) {
  for (const t of stills.split(',').map(Number)) { await page.evaluate(t => window.seek(t), t); await page.screenshot({ path: `${out}/still_${t.toFixed(1)}.jpg`, type: 'jpeg', quality: 90 }); }
  await browser.close(); process.exit(0);
}
const F = Number(fps), a = Number(from), b = to ? Number(to) : total;
const n = Math.round((b - a) * F);
const ff = spawn('ffmpeg', ['-y', '-v', 'error', '-f', 'image2pipe', '-framerate', String(F), '-c:v', 'mjpeg', '-i', '-',
  '-c:v', 'libx264', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
const t0 = Date.now();
for (let i = 0; i < n; i++) {
  await page.evaluate(t => window.seek(t), a + i / F);
  const buf = await page.screenshot({ type: 'jpeg', quality: 95 });
  if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
  if (i % 300 === 0) console.error(`frame ${i}/${n}  ${((Date.now() - t0) / 1000).toFixed(0)}s`);
}
ff.stdin.end(); await new Promise(r => ff.on('close', r)); await browser.close();
console.error('done', n, 'frames');
