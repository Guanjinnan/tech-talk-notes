// Open a YouTube video in a persistent Chrome profile (THE single login point),
// then extract:
//   - native video width/height + duration
//   - storyboard3 sheet-URL template + grid geometry + frame interval
//   - transcript (timestamped caption segments) — three-tier escalation:
//       1. fetch(json3)  — fast, zero-cost; blocked by pot-token on most videos (empty body)
//       2. fetch(xml)    — legacy fallback; same pot-token issue
//       3. DOM panel     — click "Show transcript", scrape ytd-transcript-segment-renderer
//                          THE REALISTIC PRIMARY PATH as of 2026 (pot-token era)
//     If all three fail → [NO_TRANSCRIPT]; SKILL.md routes to web-access fallback.
//
// The profile dir is reused by capture_slides.mjs, so the login here is the only one.
//
// Usage:
//   node get_meta.mjs \
//     --video=https://www.youtube.com/watch?v=ID \
//     --out=/path/meta.json \
//     --profile=/path/yt-login-profile
//
// Requires: npm i puppeteer  (channel 'chrome').

import puppeteer from 'puppeteer';
import fs from 'fs';
import os from 'os';
import path from 'path';

function arg(name, def) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.split('=').slice(1).join('=') : (process.env[name.toUpperCase()] ?? def);
}

// Durable profile (改动 G): persists the one-time login across runs so we never
// re-login. Lives in ~/.cache (NOT inside the skill dir — keeps the real-account
// session cookie out of anything that might be synced/shared). chmod 700 below.
const DEFAULT_PROFILE = path.join(os.homedir(), '.cache', 'tech-talk-notes', 'yt-profile');

const VIDEO_URL = arg('video');
const VIDEO_ID = (VIDEO_URL?.match(/[?&]v=([^&]+)/) || [])[1] || null;
const OUT = arg('out', '/tmp/meta.json');
const PROFILE_DIR = arg('profile', DEFAULT_PROFILE);
// 改动 H: transcript written alongside meta.json by default (same logged-in page,
// no second browser). Override with --transcript=path.
const TRANSCRIPT_OUT = arg('transcript', path.join(path.dirname(OUT), 'transcript.json'));
if (!VIDEO_URL) { console.error('usage: node get_meta.mjs --video=URL [--out=meta.json] [--transcript=path] [--profile=dir]'); process.exit(2); }

// Ensure the profile dir exists and is owner-only before Chrome writes the session.
fs.mkdirSync(PROFILE_DIR, { recursive: true });
try { fs.chmodSync(PROFILE_DIR, 0o700); } catch (e) { /* non-fatal on non-POSIX */ }

const browser = await puppeteer.launch({
  headless: false,
  channel: 'chrome',
  userDataDir: PROFILE_DIR,
  ignoreDefaultArgs: ['--enable-automation'],
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

console.log('\n==================================================================');
console.log('  请在弹出的 Chrome 窗口登录 YouTube，回到视频页让视频开始播放。');
console.log('  这是整个流程唯一一次登录；之后 capture_slides.mjs 复用同一 profile。');
console.log('  脚本每 3 秒检测一次，最长等待 8 分钟。');
console.log('==================================================================\n');

async function probe() {
  try {
    return await page.evaluate(() => {
      const v = document.querySelector('video');
      const spec = window.ytInitialPlayerResponse?.storyboards
        ?.playerStoryboardSpecRenderer?.spec
        || (window.ytplayer?.config?.args?.player_response
            ? (JSON.parse(window.ytplayer.config.args.player_response)
                 .storyboards?.playerStoryboardSpecRenderer?.spec) : null);
      return { dur: v?.duration || 0, w: v?.videoWidth || 0, h: v?.videoHeight || 0,
               spec: spec || null, url: location.href.slice(0, 90) };
    });
  } catch (e) { return { error: e.message }; }
}

let st = await probe();
const deadline = Date.now() + 8 * 60 * 1000;
let lastUrl = '';
while ((!st.dur || st.dur < 60 || st.w === 0 || !st.spec) && Date.now() < deadline) {
  await new Promise(r => setTimeout(r, 3000));
  st = await probe();
  if (st.url && st.url !== lastUrl) { console.log('  [cur]', st.url); lastUrl = st.url; }
  try { await page.evaluate(() => { const v=document.querySelector('video'); if(v){v.muted=true;v.play().catch(()=>{});} }); } catch(e){}
  process.stdout.write('.');
}
console.log('\nstate:', JSON.stringify({ dur: st.dur, w: st.w, h: st.h, hasSpec: !!st.spec }));

if (!st.dur || st.w === 0 || !st.spec) {
  console.error('Could not obtain video dimensions + storyboard spec. Leaving browser open.');
  process.exit(3);
}

// ── 改动 H: extract the transcript from the same logged-in page ──────────────
// Run this BEFORE storyboard parsing: the two are independent, and storyboard
// verification can fail (signature/format quirks per video) while captions are
// fine. Gating transcript behind storyboard would lose it on those videos. The
// page is live here (st.spec present → ytInitialPlayerResponse populated).
//
// YouTube exposes caption tracks in ytInitialPlayerResponse.captions. We pick an
// English track (manual over auto-generated) and fetch its json3 form IN-PAGE
// (same-origin, logged in → not bot-blocked), so no separate browser is needed.
// json3 shape: { events: [ { tStartMs, dDurationMs, segs: [ {utf8}, ... ] } ] }.
async function extractTranscript() {
  return await page.evaluate(async () => {
    const tracks = window.ytInitialPlayerResponse?.captions
      ?.playerCaptionsTracklistRenderer?.captionTracks;
    if (!tracks || !tracks.length) return { ok: false, reason: 'no-caption-tracks' };
    // Prefer English manual, then English ASR, then first track.
    const en = tracks.filter(t => (t.languageCode || '').startsWith('en'));
    const pick = en.find(t => t.kind !== 'asr') || en[0] || tracks[0];
    const base = pick.baseUrl;
    const withParam = (u, p) => u + (u.includes('?') ? '&' : '?') + p;

    // Fetch as TEXT first — YouTube sometimes returns HTTP 200 with an EMPTY body
    // (json3 increasingly gated), which crashes r.json(). Try json3, then fall back
    // to the legacy XML (srv1/srv3) form, which is returned more reliably.
    async function getText(url) {
      try {
        const r = await fetch(url);
        if (!r.ok) return { err: 'status-' + r.status };
        const t = await r.text();
        return { t };
      } catch (e) { return { err: String(e) }; }
    }

    let segments = [];
    let how = null;

    // Attempt 1: json3 → { events: [ { tStartMs, segs: [{utf8}] } ] }
    const j = await getText(withParam(base, 'fmt=json3'));
    if (j.t && j.t.trim()) {
      try {
        const data = JSON.parse(j.t);
        for (const ev of (data.events || [])) {
          if (!ev.segs) continue;
          const text = ev.segs.map(s => s.utf8 || '').join('').replace(/\s+/g, ' ').trim();
          if (!text) continue;
          segments.push({ t: Math.round((ev.tStartMs || 0) / 100) / 10, text });
        }
        if (segments.length) how = 'json3';
      } catch (e) { /* fall through to XML */ }
    }

    // Attempt 2: legacy XML → <text start="s" dur="d">escaped content</text>
    if (!segments.length) {
      const x = await getText(base); // no fmt → XML
      const body = x.t || '';
      if (body.trim()) {
        const re = /<text[^>]*\bstart="([\d.]+)"[^>]*>([\s\S]*?)<\/text>/g;
        const decode = (s) => s
          .replace(/<br\s*\/?>(?=)/gi, ' ')
          .replace(/&amp;#39;|&#39;/g, "'").replace(/&amp;quot;|&quot;/g, '"')
          .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
          .replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
        let m;
        while ((m = re.exec(body)) !== null) {
          const text = decode(m[2]);
          if (text) segments.push({ t: Math.round(parseFloat(m[1]) * 10) / 10, text });
        }
        if (segments.length) how = 'xml';
      }
    }

    if (!segments.length) {
      return { ok: false, reason: (j.err ? 'json3:' + j.err + ' ' : '') + 'empty-body-both-formats' };
    }
    return { ok: true, lang: pick.languageCode || null,
             auto: pick.kind === 'asr', name: pick.name?.simpleText || null,
             how, segments };
  });
}

// ── Tier 3: transcript panel DOM scraping (改動 J) ─────────────────────────
// pot-token blocks fetch(json3/xml) on most videos (returns 200 + empty body).
// The same logged-in page's Transcript panel is NOT gated — clicking "Show
// transcript" and scraping ytd-transcript-segment-renderer works reliably.
// Validated by web-access agent on 2026-06-30 (video -QFHIoCo-Ko, 1820 segments).
async function scrapeTranscriptPanel() {
  // 1. Expand description to reveal transcript button (may already be expanded)
  await page.evaluate(() => {
    const btn = document.querySelector('#description-inline-expander #expand')
      || document.querySelector('tp-yt-paper-button#expand')
      || document.querySelector('#expand');
    if (btn && btn.offsetParent !== null) btn.click();
  }).catch(() => {});
  await new Promise(r => setTimeout(r, 1500));

  // 2. Find and click transcript button — multiple strategies for i18n
  const clicked = await page.evaluate(() => {
    const all = [...document.querySelectorAll(
      'button, a, ytd-button-renderer, tp-yt-paper-button, [role="button"]'
    )];
    for (const el of all) {
      const t = (el.textContent || '').toLowerCase();
      const a = (el.getAttribute('aria-label') || '').toLowerCase();
      if ((/show transcript|transcript|转写文稿|转写|文稿/.test(t)
           || /show transcript|transcript|转写/.test(a))
          && el.offsetParent !== null) {
        el.click();
        return 'text-match';
      }
    }
    // Fallback: transcript section button in structured description
    const epBtns = document.querySelectorAll(
      'ytd-video-description-transcript-section-renderer button'
    );
    for (const b of epBtns) {
      if (b.offsetParent !== null) { b.click(); return 'ep-section'; }
    }
    return null;
  });
  if (!clicked) return { ok: false, reason: 'panel-button-not-found' };

  // 3. Wait for transcript segments to render
  try {
    await page.waitForSelector('ytd-transcript-segment-renderer', { timeout: 8000 });
  } catch (e) {
    return { ok: false, reason: 'panel-no-segments-after-' + clicked };
  }

  // 4. Scroll to collect all segments (some panels lazy-render)
  await new Promise(r => setTimeout(r, 1000));
  let prevCount = 0;
  for (let i = 0; i < 15; i++) {
    const count = await page.evaluate(() => {
      const c = document.querySelector('#segments-container')
        || document.querySelector('ytd-transcript-search-panel-renderer')
        || document.querySelector('ytd-transcript-renderer');
      if (c) c.scrollTop = c.scrollHeight;
      return document.querySelectorAll('ytd-transcript-segment-renderer').length;
    });
    if (count > 0 && count === prevCount) break;
    prevCount = count;
    await new Promise(r => setTimeout(r, 500));
  }

  // 5. Parse timestamp + text from each segment
  const segments = await page.evaluate(() => {
    const nodes = document.querySelectorAll('ytd-transcript-segment-renderer');
    const result = [];
    for (const node of nodes) {
      const tsEl = node.querySelector('.segment-timestamp')
        || node.querySelector('[class*="timestamp"]');
      const txtEl = node.querySelector('.segment-text')
        || node.querySelector('yt-formatted-string.segment-text')
        || node.querySelector('[class*="segment-text"]');
      if (!tsEl || !txtEl) continue;
      const parts = (tsEl.textContent || '').trim().split(':').map(s => parseInt(s, 10));
      let t;
      if (parts.length === 3) t = parts[0] * 3600 + parts[1] * 60 + parts[2];
      else if (parts.length === 2) t = parts[0] * 60 + parts[1];
      else continue;
      if (isNaN(t)) continue;
      const text = (txtEl.textContent || '').replace(/\s+/g, ' ').trim();
      if (text) result.push({ t, text });
    }
    return result;
  });

  if (!segments.length) return { ok: false, reason: 'panel-parsed-zero-segments' };
  return { ok: true, how: 'dom-panel', segments, lang: null, auto: null, name: null };
}

let tr;
try { tr = await extractTranscript(); }
catch (e) { tr = { ok: false, reason: 'eval-error: ' + e.message }; }

// Tier 3: DOM panel scraping — the realistic primary path (pot-token era)
if (!tr.ok || !tr.segments?.length) {
  console.log('  fetch tiers returned empty; trying transcript panel DOM...');
  try { tr = await scrapeTranscriptPanel(); }
  catch (e) {
    const msg = 'dom-error: ' + e.message;
    tr = { ok: false, reason: ((tr && tr.reason) || '') + ' + ' + msg };
  }
}

if (tr.ok && tr.segments.length) {
  const transcript = {
    video_id: VIDEO_ID, video_url: VIDEO_URL,
    lang: tr.lang, auto_generated: tr.auto, track_name: tr.name,
    segment_count: tr.segments.length,
    segments: tr.segments,                                  // [{t, text}]
    text: tr.segments.map(s => s.text).join(' '),           // full plain text
  };
  fs.writeFileSync(TRANSCRIPT_OUT, JSON.stringify(transcript, null, 2));
  const chars = transcript.text.length;
  console.log(`\nWrote ${TRANSCRIPT_OUT}`);
  console.log(`  transcript: ${tr.segments.length} segments, ${chars} chars` +
              `, lang=${tr.lang}${tr.auto ? ' (auto)' : ''} via ${tr.how}`);
} else {
  // 改动 H (情况二/格式没对上): do NOT fabricate. Signal clearly so SKILL.md routes
  // to the web-access transcript fallback. meta.json below still proceeds.
  console.log(`\n[NO_TRANSCRIPT] reason=${tr.reason || 'empty'} — caption-track path` +
              ` unavailable. Fall back to web-access transcript panel (see SKILL.md step 1).`);
}

// ── Parse the storyboard spec and pick/verify the highest-res level ──
// spec = baseUrl | L0seg | L1seg | ... ; baseUrl has THREE placeholders: $L (level),
// $N (a per-level name token), $M (sheet index), plus ?sqp=...
// each Lseg has exactly 8 #-fields: w#h#frameCount#cols#rows#intervalMs#N#sigh
//
// Correct URL assembly (ported from yt-dlp's maintained youtube extractor,
// _extract_storyboard): replace $L→level, then $N→the N token (args[6], which itself
// usually embeds $M, e.g. "M$M"), then $M→sheet index, and append &sigh=<args[7]> verbatim.
// Our earlier code wrongly treated $N/$M as the sheet index directly — it dropped the
// "M" prefix and mis-derived the signature, so every sheet 404'd. This is the fix.
function parseLevel(baseUrl, seg, level) {
  const f = seg.split('#');
  if (f.length !== 8) return null;                       // malformed level → skip
  const w = +f[0], h = +f[1], frameCount = +f[2], cols = +f[3], rows = +f[4], intervalMs = +f[5];
  const N = f[6], sigh = f[7];
  // Normalize the sheet-index placeholder to {N} so detect_slides.py can fill it.
  const template = (baseUrl.replace('$L', String(level)).replace('$N', N) + '&sigh=' + sigh)
                     .replace('$M', '{N}');
  return { level, w, h, frameCount, cols, rows, intervalMs, template };
}

const probeFetch = (u) => page.evaluate(async (url) => {
  try {
    const r = await fetch(url.replace('{N}', '0'), { method: 'GET' });
    const ct = r.headers.get('content-type') || '';
    return { ok: r.ok, status: r.status, isImg: ct.startsWith('image/') };
  } catch (e) { return { ok: false, status: 0, isImg: false, err: String(e) }; }
}, u);

const parts = st.spec.split('|');
const baseUrl = parts[0];
const levels = parts.slice(1).map((seg, i) => parseLevel(baseUrl, seg, i)).filter(Boolean);

let chosen = null, sheetTemplate = null;
for (const lv of levels.reverse()) {            // highest level first
  const res = await probeFetch(lv.template);
  console.log(`  L${lv.level} ${lv.w}x${lv.h} ${lv.cols}x${lv.rows}  ${lv.template.slice(0,80)}...  -> ${res.status}${res.isImg?' img':''}`);
  if (res.ok && res.isImg) { chosen = lv; sheetTemplate = lv.template; break; }
}

if (!chosen) { console.error('No storyboard level returned an image. Leaving browser open.'); process.exit(4); }

const perSheet = chosen.cols * chosen.rows;
const nsheets = Math.ceil(chosen.frameCount / perSheet);
const meta = {
  video_url: VIDEO_URL,
  video_id: VIDEO_ID,
  duration: Math.round(st.dur),
  width: st.w, height: st.h,
  storyboard: {
    level: chosen.level,
    sheet_url_template: sheetTemplate, // contains {N} for sheet index
    frame_w: chosen.w, frame_h: chosen.h,
    cols: chosen.cols, rows: chosen.rows,
    interval_s: Math.round((chosen.intervalMs || 0) / 1000) || null,
    frame_count: chosen.frameCount,
    nsheets,
  },
};
fs.writeFileSync(OUT, JSON.stringify(meta, null, 2));
console.log(`\nWrote ${OUT}`);
console.log(`  duration=${meta.duration}s  native=${meta.width}x${meta.height}`);
console.log(`  storyboard L${chosen.level} ${chosen.cols}x${chosen.rows} @ ${meta.storyboard.interval_s}s, ${nsheets} sheets`);

await browser.close();
