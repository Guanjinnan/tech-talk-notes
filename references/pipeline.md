# Pipeline reference — gotchas & internals

Read this when a step misbehaves or you need to understand why the pipeline is shaped this way.

## Table of contents
- Why this exact pipeline (dead ends already ruled out)
- Transcript acquisition (改動 H)
- Persistent profile (改動 G)
- Storyboard spec → sheet URL (get_meta.mjs)
- Slide detection heuristic (detect_slides.py)
- Dedup judgement (reading the table)
- Capture internals (capture_slides.mjs)
- CDP fallback (capture_cdp.py)
- Slide quality gate (check_slides.py)
- build_page.py + template markers
- Subfolder output layout
- JSON curly-quote pitfall
- Fan-out content authoring (改動 I)
- Troubleshooting

## Why this exact pipeline (dead ends already ruled out)

These were tried and failed; do not revisit them:
- **yt-dlp**: fully bot-blocked for these videos (no-cookies / Chrome-keychain / Safari-TCC /
  all player clients). Not viable for transcript or frames.
- **CDP-attach to the user's running Chrome**: their Chrome is not started with
  `--remote-debugging-port`, so port 9222 only answers Chrome's internal service (404 on
  `/json`). Cannot attach.
- **Copying the user's Chrome profile** to reuse cookies: blocked by a security classifier as
  credential-store extraction. Don't attempt.
- **Anonymous storyboard-spec fetch**: the watch page served without login contains only
  player feature-flags, not `playerStoryboardSpecRenderer`. The spec is only reliably present
  in the logged-in player — hence `get_meta.mjs` is the login point.
- **Embed iframe / fresh Puppeteer profile**: hit YouTube's "sign in to confirm you're not a
  bot" wall. The fix is a *persistent* profile the user logs into once.
- **canvas.drawImage() for DRM-protected players**: YouTube Widevine DRM taints the canvas —
  `drawImage()` returns a solid black rectangle. This is a browser security feature, not a
  bug. The workaround is CDP compositor capture (see CDP fallback section).

## Transcript acquisition (改動 H)

**Primary path (in-page, fast):** `get_meta.mjs` extracts captions from the same logged-in
page that provides the storyboard spec. YouTube exposes caption tracks in
`ytInitialPlayerResponse.captions.playerCaptionsTracklistRenderer.captionTracks`. The script:

1. Filters for English tracks, preferring manual (`kind !== 'asr'`) over auto-generated
2. Fetches `baseUrl + '&fmt=json3'` **in-page** (same-origin, logged in → not bot-blocked)
3. Parses `json3` format: `{ events: [{ tStartMs, dDurationMs, segs: [{utf8}] }] }`
4. Writes `transcript.json`:
   ```json
   { "video_id": "...", "lang": "en", "auto_generated": false,
     "segment_count": 412, "segments": [{"t": 0.0, "text": "..."}],
     "text": "full plain text..." }
   ```

If extraction fails (no caption tracks, fetch error, empty segments), the script prints
`[NO_TRANSCRIPT] reason=...` and does **not** write `transcript.json`. It does **not**
fabricate content. `meta.json` is still written successfully.

**Fallback (web-access, slow):** when `[NO_TRANSCRIPT]` appears in stdout, use the
`web-access` skill to open the video's "Show transcript" panel and scrape the text. This is
the original path — slower (~3–5 min for a separate browser) but handles edge cases where
the json3 API format changes or tracks are structured unusually.

**No-caption case (本轮不做):** if the video truly has no captions at all, the script should
report this clearly and stop. Future iteration: Whisper transcription or user-provided script.

## Persistent profile (改動 G)

The Chrome profile used by `get_meta.mjs` and `capture_slides.mjs` is persisted at
`~/.cache/tech-talk-notes/yt-profile` (not inside the skill directory — avoids leaking the
real-account session cookie if the skill is synced or shared).

- Created with `fs.mkdirSync({recursive: true})` + `fs.chmodSync(0o700)` (owner-only)
- The user logs in **once** with their regular YouTube account; subsequent runs skip login
- Override with `--profile=/path` if needed
- **To log out / reset**: delete the directory (`rm -rf ~/.cache/tech-talk-notes/yt-profile`)
- **Privacy**: the directory contains session cookies (equivalent to "stay signed in" in a
  browser). It must not be committed to git, synced to cloud storage, or shared. The skill
  directory itself is safe to share — the profile lives outside it.

## Storyboard spec → sheet URL (get_meta.mjs)

YouTube exposes low-res "storyboard" sprite sheets (used for seek-bar previews). The spec is
`ytInitialPlayerResponse.storyboards.playerStoryboardSpecRenderer.spec`, a `|`-delimited
string:

```
<baseUrl> | <L0 seg> | <L1 seg> | <L2 seg> | <L3 seg>
```

- `baseUrl` contains `storyboard3_L$L/$M.jpg?sqp=<SQP>` — `$L` = level index, `$M` = sheet
  number. `sqp` (shared) lives here.
- each `L<i> seg` is `#`-delimited: `frameW#frameH#frameCount#cols#rows#intervalMs#name#rs$<SIG>`.
  The per-level signature is the `rs$…` field.

`get_meta.mjs` builds the sheet-URL template as
`baseUrl.replace('$L', level).replace('$M', '{N}') + '&rs=' + SIG` and **verifies it by
fetching sheet M0 in-page** before committing; it walks levels high→low and tries both
`&rs=<SIG>` and `&sigh=rs$<SIG>` forms, picking the first that returns an image. L3 is the
highest-res (~320×180 per frame, usually a 3×3 grid). The reference video's known-good form is
`…/storyboard3_L3/M{N}.jpg?sqp=…&rs=…`.

`nsheets = ceil(frameCount / (cols*rows))`. `interval_s` comes from `intervalMs/1000` (falls
back to `duration/frameCount`).

## Slide detection heuristic (detect_slides.py)

### Auto-theme detection (改動 M)

The script auto-detects whether the talk uses light-background or dark-background slides, then
applies the matching heuristic. This is necessary because dark-themed code talks (black bg,
white text) produce 0 detections under the white-slide heuristic — all frames have wf < 0.18
and brightness < 50.

**Theme decision** (after computing stats for all frames):
- `light` if ≥10% of frames pass the white-slide test
- `dark` if ≥15% of frames pass the dark-slide test (and light didn't trigger)
- Default: `light`

Why not OR both paths: white-background talks can have high-variance speaker shots that
accidentally trigger the dark-slide heuristic, creating false positives. Auto-detecting first
is safer.

### Light theme (white-background PPT)

`is_slide = white>0.28 and purple<0.12 and brightness>120`

The discriminator is the **purple-pixel fraction**: conference stage lighting reads as purple
(`R>70 and B>70 and G < 0.72·R and G < 0.72·B`). Slides are near-0% purple, bright, mostly
white. Transition threshold: `fdiff > 18`. Representative: highest white-fraction frame.

### Dark theme (black-background code talks)

`is_slide = var>1500 and tf>0.10 and purple<0.12`

Two signals replace white-fraction:
- **`tf` (text fraction)**: grayscale pixels > 150 (bright text/graphics on dark background).
  Dark slides: tf > 0.15; speaker shots: tf < 0.08. Clean gap in between.
- **`var` (color variance)**: per-pixel RGB variance on a 16×9 downsample. Structured content
  (text on solid background) produces high variance (>3000); organic speaker shots are smooth
  (<1000). Thresholds are set conservatively below the observed gap.

Transition threshold: `fdiff > 10` (dark slides have subtler transitions than white slides).
Representative: highest text-fraction frame.

### Common to both themes

Frames are segmented on visual transitions; a segment that is ≥50% slide-frames becomes one
slide instance. The `sigDiff_prev` column in the output uses a 16×9 grayscale perceptual
signature and is independent of the theme — the dedup agent uses it the same way for both.

## Dedup judgement (reading the table)

`detect_slides.py` prints `sigDiff_prev` = perceptual distance from the previous instance's
representative frame. Use it to decide what to keep:
- **Small `sigDiff_prev` (≈0–5)** → the same slide re-detected (e.g. a build/animation, or the
  speaker returned to it). Keep one, drop the rest.
- **Mid/large `sigDiff_prev`** → a genuinely different slide. Keep.
- **Low white-fraction rows clustered together** (e.g. an animated/transition sequence) → keep
  one representative or skip; don't ship five near-identical frames of one animation.
- Cross-check ambiguous rows against the transcript timestamp to confirm it's a slide moment.

Then write `slide_manifest.json` with one entry per kept slide. `cap_t` defaults to the row's
`t_rep`; nudge ± a few seconds if that moment is mid-transition.

## Capture internals (capture_slides.mjs)

### Quality forcing (ABR workaround)

YouTube's Adaptive Bitrate player starts at the lowest quality (often 144p). Without
intervention, `video.videoWidth` reflects this low resolution and the canvas capture is
blurry. The script forces the highest available quality before capturing:

1. **Set quality range:** `document.getElementById('movie_player').setPlaybackQualityRange(best, best)`
   where `best` is `getAvailableQualityLevels()[0]` (e.g. `hd1080`).
2. **Per-frame HD poll:** after each seek + `seeked` event + 2 rAF, the script polls
   `video.videoWidth` every 100ms (up to 5s) until it reaches the expected minimum width
   for the forced quality level (e.g. 1920px for `hd1080`, 1280px for `hd720`).
3. **Degrade-and-warn:** if `videoWidth` never reaches the target within the timeout, the
   frame is captured at whatever resolution is available and logged with `WARN:below-target`.
   This is better than failing the frame entirely.

### Capture mechanics

Seeks the `<video>` to `cap_t`, waits for the `seeked` event + two `requestAnimationFrame`s
(ensures the frame is painted), then polls for HD resolution (see above), draws to a canvas
at `videoWidth×videoHeight`, and saves `toDataURL('image/png')`. Conference talks are clear
MSE (no DRM), so the canvas does not taint. Black-frame guard: samples a center 12×12 patch;
if all-black it nudges `cap_t` by +2s and retries up to 3×. Anti-bot: persistent profile,
`ignoreDefaultArgs:['--enable-automation']`, `navigator.webdriver` spoof.

## CDP fallback (capture_cdp.py)

When Puppeteer capture fails (login wall, DRM black frames, bot detection), use the CDP
fallback via the `web-access` skill's browser proxy.

### Why CDP works where canvas fails

YouTube uses Widevine DRM for some content. Browsers enforce that DRM-protected `<video>`
elements taint any canvas they're drawn to — `drawImage()` produces a black rectangle. CDP's
`Page.captureScreenshot` captures at the compositor level (the same pipeline the GPU uses to
paint the display), so it sees the actual rendered frame regardless of DRM status.

### How to use it

1. Ensure the `web-access` skill is running and has navigated to the YouTube video
2. The CDP proxy is available at `http://localhost:3456` by default
3. Run capture_cdp.py:
   ```
   python3 "$SK/scripts/capture_cdp.py" --video=<URL> --manifest=slide_manifest.json \
        --out=slides_hd [--crop=LEFT,TOP,RIGHT,BOTTOM]
   ```

### Crop box calibration

The CDP screenshot captures the entire viewport, not just the video. The video region must
be cropped with PIL. Coordinates are in **physical pixels** (CSS × DPR).

On a DPR=2 Retina display with a typical YouTube layout, the empirically measured crop box is:
```
(348, 112, 2524, 1336)   → 2176×1224 physical pixels → 1088×612 CSS pixels
```

This will vary with:
- Display DPR (1× vs 2× vs 3×)
- Browser chrome height (tabs, bookmarks bar)
- YouTube theater mode vs default mode
- Window size

To calibrate for a new setup:
1. Take a CDP screenshot without cropping
2. Open in an image editor, measure the video region in pixels
3. Pass `--crop=LEFT,TOP,RIGHT,BOTTOM`

### Failure scenarios and the 10-minute rule

| Scenario | Symptom | Action |
|----------|---------|--------|
| Puppeteer login wall | Chrome opens but YouTube shows "Sign in" loop | Switch to CDP after 10 min |
| Puppeteer black frames | PNGs are solid black | DRM taint — switch to CDP |
| Puppeteer bot detection | "Confirm you're not a bot" page | Switch to CDP after 10 min |
| CDP proxy not running | `Cannot reach CDP proxy` error | Start web-access skill first |
| CDP black frames | Still black after crop | Wrong crop box — recalibrate |
| Both paths fail after 10 min each | — | Ask user for manual screenshots |

## Slide quality gate (check_slides.py)

Automated rubric scoring replaces manual visual review of every slide.

### Metrics

| Metric | What it measures | Good slide | Bad sign |
|--------|-----------------|------------|----------|
| `width` (pixels) | Capture resolution | ≥ 1280 (HD) | < 1280 → ABR served a low-quality frame |
| `wf` (white fraction) | % white pixels | > 0.28 | < 0.10 → dark/camera frame |
| `pf` (purple fraction) | % purple pixels (stage lighting) | < 0.12 | > 0.12 → speaker shot |
| `brightness` | Mean luminance | > 120 | < 100 → dark frame |
| `sigDiff_prev` | Perceptual diff from previous | > 3.0 | < 3.0 → duplicate |

### Flags

- `low-resolution`: width < `--min-width` (default 1280) — capture ran below HD. This is the
  backstop for the ABR-starts-at-144p bug: even if the capture script's HD forcing silently
  degraded, a sub-HD frame flips the verdict to `needs-review` here rather than shipping blurry.
- `suspect-speaker-shot`: pf ≥ 0.12 — likely a camera-on-speaker frame
- `suspect-dark-frame`: wf < 0.10 and brightness < 100
- `suspect-stage-wide`: low white + some purple — wide stage shot
- `suspect-duplicate`: sigDiff < 3.0 from previous slide

`summary.min_captured_width` reports the worst resolution across all slides — a one-glance
health check without opening any image.

### Outputs

- `capture_report.json`: machine-readable report with per-slide scores
- `contact_sheet.png`: visual thumbnail grid (6 columns)

The agent should read `capture_report.json` and only inspect flagged slides, not all slides.

## build_page.py + template markers

`assets/template.html` is the current notes page with the dynamic parts replaced by markers:
- scalars `{{PAGE_TITLE}}`, `{{TOPBAR}}`, `{{HERO_LABEL}}`, `{{TITLE_HTML}}`,
  `{{SUBTITLE_EN}}`, `{{SUBTITLE_ZH}}`, `{{SPEAKERS}}`, `{{SLIDES_DESC}}`, `{{QA_DESC}}`,
  `{{INSIGHTS_DESC}}`, `{{SOURCE_URL}}`, `{{SOURCE_TITLE}}`, `{{FOOTER_NOTE}}`
- card lists `<!-- SLIDES -->`, `<!-- QA_ITEMS -->`, `<!-- INSIGHTS -->`
- the whole Q&A section is wrapped in `<!--QA_SECTION_START-->…<!--QA_SECTION_END-->` and its
  nav link in `<!--QA_NAV_START-->…<!--QA_NAV_END-->`; when `content.qa` is empty, both are
  stripped (no orphan nav link or empty section).

All CSS, the lightbox (HTML/CSS/JS), nav, and footer are byte-identical to the original notes —
that is the locked style. To change the look, edit `assets/template.html`, not the script.

## Subfolder output layout

`build_page.py` outputs to a slug-based subfolder, not the project root:

```
<talk-slug>/
  index.html          ← notes page (stamped with <!--tech-talk-notes-generated-->)
  slides_hd/          ← captured PNGs copied here
    01.png, 02.png, ...
```

The slug is derived from `meta.page_title` (lowercased, stripped of special chars, truncated
to 60 chars). Override with an explicit second argument: `build_page.py content.json my-dir`.

**Overwrite protection**: if `index.html` exists and does NOT contain the generation marker,
the script aborts with an error. This prevents silently overwriting another talk or a
hand-written page.

## JSON curly-quote pitfall

`content.json` is hand-authored by Claude. A common failure mode: ASCII double-quotes `"`
inside a JSON string value break the parse. Example:

```json
{ "en": "He said "hello" to the crowd" }     ← BROKEN: unescaped inner quotes
{ "en": "He said “hello” to the crowd" }   ← OK: curly quotes (not special in JSON)
{ "en": "He said 'hello' to the crowd" }     ← OK: single quotes
{ "en": "He said \"hello\" to the crowd" }   ← OK: escaped
```

**Rule**: use curly/smart quotes `""` or single quotes for quoted speech within caption text.
Never use bare ASCII `"` inside a JSON string value.

`build_page.py` now gives a friendly error with line number, column, and surrounding context
when JSON parsing fails.

**Validation gate**: always run `python3 -c "import json; json.load(open('content.json'))"` 
immediately after writing `content.json`, before calling `build_page.py`.

## Fan-out content authoring (改動 I + 改動 K)

### Why parallel

Serial authoring of 30 slide captions + Q&A + insights in one context takes 15–25 minutes
and inflates the main context with repetitive slide-by-slide work. Each caption is
independent (needs only its slide image + a transcript window), so they parallelize naturally.

### Architecture (改動 K: digest-first + grouped fan-out)

```
transcript.json ready ─── digest agent (sonnet) ──── digest.json
                                │
                    ┌───────────┼───────────────────┐
                    │           │                   │
                    │    Q&A agent ◄─ digest.qa_questions
                    │    + targeted transcript windows
                    │           │                   │
                    │    Insights agent ◄─ digest.section_outline
                    │    + digest.term_table         │
                    │           │                   │
slide group A ─── caption group agent A ◄─ digest.term_table
slide group B ─── caption group agent B    + ±40s transcript windows
  ...                          │                   │
                         all captions              │
                               │                   │
                          ┌────┴───────────────────┘
                          │
                    Merge + consistency pass (opus)
                          │
                    content.json → build
```

Key difference from the original 改動 I architecture: the **digest agent** sits between the
raw transcript and all writing agents. The full transcript text enters exactly one agent
(the digest). All downstream agents receive only the structured digest + targeted transcript
windows around their specific timestamps.

### Digest agent (改動 K — the context-explosion fix)

The digest agent reads `transcript.json` once and returns:
- `section_outline[]` — topic boundaries with timestamps
- `term_table{}` — canonical terminology mappings
- `speakers[]` — names and roles
- `qa_questions[]` — each with timestamp, asker, topic
- `qa_boundary` — seconds where slides end and Q&A begins

This is the root fix for insights-agent timeout: the insights agent previously chewed all
1820 raw segments; now it receives only the structured outline + term table.

### Grouped fan-out (改動 K — replaces one-slide-per-agent)

Per-slide captions are batched **3–5 slides per agent** instead of one-per-agent. Benefits:
- Reduces agent startup overhead (30 agents → 6–8 agents)
- Each agent sees neighboring slides, improving local coherence
- The consistency pass still catches cross-group drift

Each group agent receives: its slide images, ±40s transcript windows per slide, and the
shared style brief (including the digest's term_table).

### Shared style brief

Every writing sub-agent receives an identical style brief (defined in SKILL.md step 4b) to
prevent drift. The brief covers: voice, bilingual standards, formatting rules (bold, code),
JSON quote rules, and a terminology table from the **digest** (not the raw transcript).

### Consistency pass

The merge agent (running at `opus` tier) reviews the assembled `content.json` for:
- **Terminology drift**: same concept named differently across slides
- **Tonal inconsistency**: one slide academic, another casual
- **Adjacent duplicates**: parallel workers sometimes converge on similar phrasing
- **Format drift**: inconsistent bold/code usage
- **Reference integrity**: Q&A/insights citing slide numbers that shifted during dedup

This pass is mandatory (~1.5 min) and is the quality backstop that makes fan-out safe.

### Resilience rules (改動 K — hard rules)

- All `pipeline()`/`parallel()` results must be `.filter(Boolean)` before use.
- **Single-agent failure → retry that agent only.** Never fall back to writing `content.json`
  by hand in the main context. That path caused the context explosion in the 改動 I实跑 —
  it is now explicitly forbidden.
- If a retry also fails → skip that item, note the gap in the consistency pass.

### Model allocation

| Role | Model | Rationale |
|------|-------|-----------|
| Digest | sonnet | One-shot structured extraction, fast |
| Per-slide group | sonnet | Narrow task, style-brief-constrained, consistency pass catches drift |
| Q&A | sonnet | Structured extraction from digest + transcript windows |
| Insights | sonnet | Synthesis from digest (not raw transcript), grounded in timestamps |
| Consistency pass | opus | Cross-slide judgement, no mechanical gate, quality backstop |

Sub-agent models are set via `Agent({model: 'sonnet'})` or `Workflow agent()` opts. The main
loop model is the user's choice — we only auto-assign spawned sub-agents.

## Troubleshooting

- **get_meta times out / no spec**: ensure the user actually pressed play after logging in; the
  spec only appears once the player initializes. If it still fails, the account/video may be
  age- or region-gated — confirm the video plays normally in that window.
- **detect fetches 0 frames**: the verified template in `meta.json` may have expired (sqp/rs are
  time-limited). Re-run `get_meta.mjs` to refresh `meta.json`, then re-run detect promptly.
- **A captured PNG is the wrong frame / a speaker**: adjust that entry's `cap_t` in
  `slide_manifest.json` and re-run capture (it overwrites by index).
- **Build looks unstyled**: you edited the template and broke a marker, or a `{{…}}` leaked —
  grep the output for `{{` and for the marker comments; both should be absent.
- **build_page.py refuses to write**: the target `index.html` exists and was not generated by
  this script. Either remove it manually or pass a different output dir.
- **Puppeteer capture returns black frames**: likely DRM taint. Switch to CDP fallback
  (`capture_cdp.py`). See § CDP fallback above.
- **CDP screenshot has wrong crop**: the video region shifted. Take an uncropped screenshot,
  measure coordinates, pass `--crop=L,T,R,B`.
- **content.json parse error**: check for unescaped ASCII double-quotes inside string values.
  Use curly quotes `""` or single quotes instead. The error message shows the exact line/column.
