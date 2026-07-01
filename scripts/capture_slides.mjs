// Capture native-resolution slide frames from a YouTube video via Puppeteer.
//
// Reuses a persistent Chrome profile so the YouTube login done in get_meta.mjs
// carries over — no second login. Seeks to each manifest timestamp, draws the
// <video> onto a canvas at videoWidth×videoHeight, and saves a PNG.
//
// Usage:
//   node capture_slides.mjs \
//     --video=https://www.youtube.com/watch?v=ID \
//     --manifest=/path/slide_manifest.json \
//     --out=/path/slides_hd \
//     --profile=/path/yt-login-profile
//
// manifest: JSON array of { "idx": 0-based, "cap_t": seconds } (extra keys ignored).
//
// Requires: npm i puppeteer  (channel 'chrome' must be installed).

import puppeteer from 'puppeteer';
import fs from 'fs';
import os from 'os';
import path from 'path';

const QUALITY_MIN_WIDTH = {
  highres: 2560, hd2160: 3840, hd1440: 2560,
  hd1080: 1920, hd720: 1280, large: 854,
  medium: 640, small: 426, tiny: 256,
};

function arg(name, def) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.split('=').slice(1).join('=') : (process.env[name.toUpperCase()] ?? def);
}

// Durable profile (改动 G): must match get_meta.mjs so the login carries over.
const DEFAULT_PROFILE = path.join(os.homedir(), '.cache', 'tech-talk-notes', 'yt-profile');

const VIDEO_URL  = arg('video');
const MANIFEST   = arg('manifest');
const OUT_DIR    = arg('out');
const PROFILE_DIR = arg('profile', DEFAULT_PROFILE);

if (!VIDEO_URL || !MANIFEST || !OUT_DIR) {
  console.error('usage: node capture_slides.mjs --video=URL --manifest=manifest.json --out=dir [--profile=dir]');
  process.exit(2);
}

const targets = JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
fs.mkdirSync(OUT_DIR, { recursive: true });
fs.mkdirSync(PROFILE_DIR, { recursive: true });
try { fs.chmodSync(PROFILE_DIR, 0o700); } catch (e) { /* non-fatal on non-POSIX */ }

const browser = await puppeteer.launch({
  headless: false,
  channel: 'chrome',
  userDataDir: PROFILE_DIR,
  ignoreDefaultArgs: ['--enable-automation'], // avoid Google "insecure browser" block
  args: [
    '--no-first-run', '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled',
    '--window-size=1500,950', '--start-maximized',
    '--autoplay-policy=no-user-gesture-required',
  ],
  defaultViewport: null,
});

const page = (await browser.pages())[0] || await browser.newPage();
await page.evaluateOnNewDocument(() => {
  Object.defineProperty(navigator, 'webdriver', { get: () => false });
});

console.log('Opening video page...');
await page.goto(VIDEO_URL, { waitUntil: 'networkidle2', timeout: 60000 })
  .catch(e => console.log('nav warn:', e.message));

async function getState() {
  try {
    return await page.evaluate(() => {
      const v = document.querySelector('video');
      return { hasVideo: !!v, duration: v?.duration || 0, w: v?.videoWidth || 0,
               url: location.href.slice(0, 90) };
    });
  } catch (e) { return { error: e.message }; }
}

console.log('\n==================================================================');
console.log('  若弹出登录页，请在该 Chrome 窗口登录 YouTube，然后回到视频页让其开始播放。');
console.log('  （若 get_meta.mjs 已登录过同一 profile，这里通常会直接就绪。）');
console.log('  脚本每 3 秒自动检测一次，最长等待 8 分钟。');
console.log('==================================================================\n');

let st = await getState();
const deadline = Date.now() + 8 * 60 * 1000;
let lastUrl = '';
while ((!st.duration || st.duration < 60 || st.w === 0) && Date.now() < deadline) {
  await new Promise(r => setTimeout(r, 3000));
  st = await getState();
  if (st.url && st.url !== lastUrl) { console.log('  [cur]', st.url); lastUrl = st.url; }
  try { await page.evaluate(() => { const v=document.querySelector('video'); if(v){v.muted=true;v.play().catch(()=>{});} }); } catch(e){}
  process.stdout.write('.');
}
console.log('\nFinal state:', JSON.stringify(st));

if (!st.duration || st.duration < 60 || st.w === 0) {
  console.error('Timed out waiting for a ready video. Leaving browser open for inspection.');
  await page.screenshot({ path: path.join(OUT_DIR, '_debug.png') }).catch(()=>{});
  process.exit(3);
}

console.log(`Video ready (${st.w}px wide). Forcing highest quality...`);
await page.evaluate(() => { const v=document.querySelector('video'); if(v){v.muted=true;v.pause();} });

const qualityInfo = await page.evaluate(() => {
  const player = document.getElementById('movie_player');
  if (!player?.getAvailableQualityLevels) return { error: 'no player API' };
  const levels = player.getAvailableQualityLevels();
  const best = levels[0];
  if (player.setPlaybackQualityRange) player.setPlaybackQualityRange(best, best);
  if (player.setPlaybackQuality) player.setPlaybackQuality(best);
  return { best, levels };
});
console.log('Quality set:', JSON.stringify(qualityInfo));

const targetWidth = (qualityInfo.best && QUALITY_MIN_WIDTH[qualityInfo.best]) || 1280;
console.log(`Target videoWidth: >= ${targetWidth}px`);

// Give the player a moment to switch quality before first seek
await new Promise(r => setTimeout(r, 2000));

async function captureFrame(timeSec, idx) {
  const padded = String(idx + 1).padStart(2, '0');
  const outFile = path.join(OUT_DIR, `${padded}.png`);
  for (let attempt = 0; attempt < 3; attempt++) {
    const t = timeSec + attempt * 2; // black-frame nudge
    let result;
    try {
      result = await page.evaluate(async (tt, minW) => {
        const video = document.querySelector('video');
        if (!video) return { error: 'no video' };
        video.muted = true; video.pause(); video.currentTime = tt;
        await new Promise((resolve) => {
          const to = setTimeout(resolve, 12000);
          video.addEventListener('seeked', () => { clearTimeout(to);
            requestAnimationFrame(() => requestAnimationFrame(resolve)); }, { once: true });
        });
        // Poll until videoWidth reaches HD target (up to 5s)
        let polls = 0;
        while (video.videoWidth < minW && polls < 50) {
          await new Promise(r => setTimeout(r, 100));
          polls++;
        }
        const w = video.videoWidth, h = video.videoHeight;
        const hdOk = w >= minW;
        if (!w || !h) return { error: 'no dimensions' };
        const c = document.createElement('canvas'); c.width = w; c.height = h;
        const ctx = c.getContext('2d');
        try { ctx.drawImage(video, 0, 0, w, h); } catch (e) { return { error: 'draw: '+e.message }; }
        let s; try { s = ctx.getImageData(Math.floor(w/2), Math.floor(h/2), 12, 12).data; }
        catch (e) { return { error: 'tainted: '+e.message }; }
        let black = true;
        for (let i=0;i<s.length;i+=4){ if(s[i]>8||s[i+1]>8||s[i+2]>8){black=false;break;} }
        if (black) return { error: 'black' };
        return { dataUrl: c.toDataURL('image/png'), w, h, hdOk, polls };
      }, t, targetWidth);
    } catch (e) { result = { error: 'evalfail: '+e.message }; }
    if (result.error) { if (attempt === 2) { console.error(`  ${padded} @ ${timeSec}s FAILED: ${result.error}`); return false; } continue; }
    fs.writeFileSync(outFile, Buffer.from(result.dataUrl.replace(/^data:image\/png;base64,/,''), 'base64'));
    const warn = result.hdOk ? '' : ' WARN:below-target';
    console.log(`  ${padded}.png  (${result.w}x${result.h}) @ ${timeSec + attempt*2}s  polls=${result.polls}${warn}`);
    return true;
  }
  return false;
}

let ok=0, bad=0;
for (const s of targets) { (await captureFrame(s.cap_t, s.idx)) ? ok++ : bad++; await new Promise(r=>setTimeout(r,250)); }
console.log(`\nDone: ${ok} captured, ${bad} failed`);
await browser.close();
