---
name: tech-talk-notes
description: Turn a YouTube tech-talk / conference / workshop video into a bilingual (Chinese + English) HTML study-notes page in a fixed Monocle editorial style. Produces three sections — every presentation slide captured at native resolution with CN/EN captions, the Q&A organized bilingually, and core insights/methods synthesized with in-video timestamps — plus a click-to-zoom + fullscreen lightbox. Use when the user gives a YouTube talk URL and wants slide screenshots, bilingual notes, Q&A write-up, or a "video learning page / 视频学习笔记 / 演讲幻灯片整理" from it.
---

# Tech Talk → Bilingual HTML Notes

Build a bilingual study-notes page from a YouTube talk: all slides (native-res screenshots
+ CN/EN captions), the Q&A (bilingual), and core insights (with timestamps). The visual
style is fixed — it lives in `assets/template.html` and must not be re-derived.

## Methodology

This pipeline's design draws from two talks:
- **Pocock (vertical slices / TDD feedback loops)**: tracer-bullet validation, subfolder
  isolation, fail-fast at each stage before committing to batch work.
- **Cherny (adversarial evaluator / gradable rubrics / structured handoffs)**: automated
  quality gate with scored rubrics, deterministic fallback scripts, delete scaffolding
  when the model catches up.

## What is automated vs. authored

- **Scripts (bundled, deterministic)** handle the fragile mechanics: storyboard slide
  detection, native-resolution capture, quality gating, and HTML assembly.
- **Claude (you) author** the high-value content: bilingual slide captions, the Q&A
  write-up, and insight synthesis. This is the part that makes the notes good — do it well.

## Constraints (validated; do not substitute unproven shortcuts)

- **YouTube only.** The pipeline relies on YouTube storyboards + the watch-page player.
- **`yt-dlp` is bot-blocked** for these videos — do not rely on it for transcript or frames.
- **One-time login, persistent profile (改動 G).** `get_meta.mjs` opens a Chrome window with
  a persistent profile at `~/.cache/tech-talk-notes/yt-profile`. First run: user logs in once.
  Subsequent runs: login carries over automatically. `capture_slides.mjs` reuses the same
  profile. Never ask the user to log in if the profile already exists.
- Requires Node `puppeteer` (Chrome channel) and Python `pillow`, `numpy`.
- **10-minute timeout rule** (Cherny: structured handoffs): if any single step's debugging
  exceeds 10 minutes wall-clock, stop and switch to the documented fallback. Do not
  hero-debug — the fallback exists for a reason.

## Workflow

Run from a working directory for this video (e.g. the project folder). Keep `meta.json`,
`slide_instances.json`, `slide_manifest.json`, `content.json` there; put PNGs in `slides_hd/`.
Let `SK=~/.claude/skills/tech-talk-notes` (resolve the real path).

### 1. Meta + login + transcript (the single login point — 改动 G/H)
```
node "$SK/scripts/get_meta.mjs" --video=<URL> --out=meta.json
```
A Chrome window opens with a **persistent profile** (`~/.cache/tech-talk-notes/yt-profile`,
auto-created, `chmod 700`). First run: tell the user to log into YouTube and let the video
play. Subsequent runs: the login carries over — no re-authentication needed.

The script writes **two** files:
- `meta.json` — native W/H, duration, verified storyboard sheet-URL template
- `transcript.json` — timestamped caption segments extracted from the same page (no second
  browser needed)

**If transcript extraction fails** (stdout contains `[NO_TRANSCRIPT]`): fall back to a
**sub-agent** using the `web-access` skill to open the video's transcript panel and return:
- Full transcript text (or path to file)
- Boundary timestamp: where slides end and Q&A begins
- Speaker names and roles mentioned

This fallback is slower (~3–5 min) but ensures no video is blocked by missing captions.

**If transcript succeeds**: spawn a **sub-agent** to read `transcript.json` and return a
structured summary (Q&A boundary, speaker names, key topics) — the full transcript stays
out of the main context.

### 2. Detect candidate slides
```
python3 "$SK/scripts/detect_slides.py" --meta meta.json --out slide_instances.json
```
This auto-detects the slide theme (light/dark) and prints a table of slide instances with
timestamps, white/text fraction, color variance, and signature-diff-from-previous. Dark-themed
talks (black bg, white text) use text-fraction + color-variance signals instead of
white-fraction (改動 M). **Delegate dedup judgement to a sub-agent** (改動 F): pass it the table
output and have it return the curated `slide_manifest.json`. The sub-agent should follow the
dedup rules in `references/pipeline.md`.

```json
[ { "idx": 0, "cap_t": 60 }, { "idx": 1, "cap_t": 128 }, ... ]
```
`idx` is the 0-based output index (drives the PNG filename `01.png`, `02.png`, …); `cap_t`
is the capture time in seconds (use the row's `t_rep`, or nudge a few seconds for a cleaner
frame).

### 3. Capture native-resolution frames

**Primary path — Puppeteer:**
```
node "$SK/scripts/capture_slides.mjs" --video=<URL> --manifest=slide_manifest.json \
     --out=slides_hd
```
Reuses the persistent profile from step 1 (no new login). Writes `slides_hd/NN.png` at
native resolution. **Note:** native resolution is not automatic — YouTube's ABR player starts
at 144p, so the script forces the highest available quality (`setPlaybackQualityRange`) and
polls `videoWidth` after each seek until the HD buffer arrives. See `references/pipeline.md §
Capture internals`.

**Fallback path — CDP proxy** (改動 B; triggers after 10-min timeout on primary):
```
python3 "$SK/scripts/capture_cdp.py" --video=<URL> --manifest=slide_manifest.json \
     --out=slides_hd [--crop=LEFT,TOP,RIGHT,BOTTOM]
```
Uses `web-access` skill's CDP proxy (`http://localhost:3456`). CDP compositor capture
bypasses YouTube DRM (canvas.drawImage returns black; CDP screenshot does not). The default
crop box is calibrated for DPR=2 Retina; pass `--crop` to override if the viewport differs.
See `references/pipeline.md § CDP fallback` for details.

**Decision tree:**
1. Try Puppeteer capture (reuses persistent profile — no login needed)
2. If login wall / black frames / bot wall after 10 min → switch to CDP fallback
3. If CDP also fails after 10 min → stop, ask the user for manual screenshots

### 3b. Quality gate (改動 C — Cherny: gradable rubrics)
```
python3 "$SK/scripts/check_slides.py" --dir=slides_hd --out=capture_report.json \
     --sheet=contact_sheet.png
```
Scores every PNG on resolution, white-fraction, purple-fraction, brightness, and perceptual
diff from previous. Outputs:
- `capture_report.json` — per-slide scores + flags (`low-resolution`, `suspect-duplicate`,
  `suspect-speaker-shot`, `suspect-dark-frame`)
- `contact_sheet.png` — thumbnail grid

A sub-HD capture (width < 1280, override with `--min-width`) is flagged `low-resolution` and
flips `summary.verdict` to `"needs-review"` — this is the backstop that catches an ABR quality
regression the capture stdout might have missed. `summary.min_captured_width` shows the worst
resolution at a glance.

**Read `capture_report.json`**, not the images. If `summary.verdict` is `"needs-review"`,
address only the flagged slides (re-capture or remove). Do not eyeball all slides manually.

### 3c. Tracer-bullet validation (改動 D — Pocock: TDD feedback loops)

Before batch-capturing all slides, run a **tracer bullet** with the first 3 slides:
1. Create a minimal `slide_manifest.json` with only entries 0–2
2. Capture those 3 slides (step 3)
3. Run quality gate (step 3b) — confirm they pass **and are HD (≥1280px wide)**. The gate now
   flags `low-resolution` automatically, so a quality regression is caught here on 3 slides,
   not by eyeball on 30.
4. Write a minimal `content.json` with those 3 slides + placeholder insights
5. Run `build_page.py` — confirm the output renders correctly
6. Only then: expand manifest to all slides and batch-capture the rest

This catches pipeline/style issues on 3 slides instead of 30.

### 4. Author content — fan-out pipeline (改動 I + 改動 K)

Content creation is the biggest time sink when done serially. Split it into parallel streams
that overlap with capture.

#### 4a. Digest-first (改動 K — mandatory, runs before any writing agent)

As soon as `transcript.json` is ready, spawn **one digest sub-agent** (model: `sonnet`) that
reads the full transcript **once** and returns a structured summary JSON:

```jsonc
{
  "section_outline": [{"t": 0, "title": "Intro"}, ...],
  "term_table": {"ReAct": "ReAct (not React)", ...},
  "speakers": [{"name": "...", "role": "..."}],
  "qa_questions": [{"t": 2400, "asker": "...", "topic": "..."}],
  "qa_boundary": 2350   // seconds — where slides end and Q&A begins
}
```

The full transcript enters **only this one agent** and **never** the main context. All
downstream writing agents consume this digest, not the raw transcript.

#### 4b. Shared style brief (inject into every writing sub-agent)

Every sub-agent that writes content receives this brief as part of its prompt:

> **Voice**: concise expert notes, not a transcript dump. 1–3 sentences per slide explaining
> what it shows and why it matters.
> **Bilingual**: Chinese and English, parallel quality — Chinese is not a translation of
> English but independently fluent.
> **Formatting**: use `<strong>` for key terms, numbers, and tool/framework names. Use
> `<code>` for code identifiers. Use `<em>` sparingly for emphasis.
> **Quotes in JSON**: use curly quotes `""` or single quotes for quoted speech. Never use
> bare ASCII `"` inside string values — it breaks JSON.
> **Terminology consistency**: inject the `term_table` from the digest (step 4a).

#### 4c. Parallel streams

These run concurrently — use `pipeline()` or parallel `Agent` calls:

1. **Per-slide captions** (grouped fan-out, model: `sonnet`): group slides into batches of
   **3–5 slides per agent** (not one-per-slide — reduces startup overhead). Each group agent
   receives:
   - Its slide images
   - The ±40s transcript window around each slide's timestamp (extracted from
     `transcript.json` by the caller — a targeted window, **not** the full text)
   - The shared style brief (including `term_table` from digest)
   - Returns: `[{ "img", "time", "title", "en", "zh" }, ...]` for its batch

2. **Q&A** (one agent, model: `sonnet`): receives the digest's `qa_questions` list + targeted
   transcript windows around each question's timestamp — **not the full transcript**. Returns
   the full `qa` array.

3. **Insights** (one agent, model: `sonnet`): receives the digest's `section_outline` +
   `term_table` + topic summary — **not the full raw transcript** (this is the root fix for
   insights agent timeout). Returns the full `insights` array.

#### 4d. Resilience rules (改動 K — hard rules, never override)

- Results from `pipeline()`/`parallel()` must be `.filter(Boolean)` before use.
- If any single writing agent fails → **retry that one agent only**. Never fall back to
  writing `content.json` by hand in the main context — that is the context-explosion failure
  mode this architecture exists to prevent.
- If a retry also fails → log the failure, skip that item, and note the gap in the
  consistency pass. Do not escalate to main context.

#### Merge + consistency pass (model: `opus`)

Once all streams complete:

1. **Assemble** `content.json` from the returned fragments + `meta` (from step 1 summary).

2. **JSON validation gate** (改動 E):
   ```
   python3 -c "import json; json.load(open('content.json'))"
   ```
   Fix any parse errors before continuing.

3. **Consistency pass** (mandatory, ~1.5 min): review the assembled `content.json` for:
   - Terminology drift across slides (e.g. one says "ReAct" another says "React")
   - Tonal inconsistency (one slide is academic, another is casual)
   - Adjacent slides with near-identical captions (parallel workers can converge)
   - Bold/formatting inconsistency
   - Q&A/insights referencing slide numbers that shifted during dedup
   Apply fixes directly to `content.json`.

4. **Build**:
   ```
   python3 "$SK/scripts/build_page.py" content.json
   ```
   Outputs to `<slug>/index.html` + `<slug>/slides_hd/` (改動 A). The slug is derived from
   `meta.page_title`.

5. **Verify** with a preview server (`python3 -m http.server 8765`): all slides load,
   lightbox works, arrows / Esc / fullscreen work.

#### Model allocation rationale

- **Writing workers** (per-slide, Q&A, insights) → `sonnet`: narrow focused tasks with a
  shared style brief + consistency pass as safety net. Cost-efficient at 30× fan-out.
- **Consistency pass** → `opus` (or the main-loop model): cross-slide judgement with no
  mechanical validation gate — this is the quality backstop, use the strongest model.
- The main loop's own model is the user's lever — we only auto-assign models to sub-agents
  we spawn.

## Output layout (改動 A — Pocock: vertical slices)

Each talk produces a self-contained subfolder:
```
<talk-slug>/
  index.html        ← the notes page
  slides_hd/        ← captured PNGs
    01.png
    02.png
    ...
```
This prevents overwriting other talks' `index.html`. The slug is auto-derived from
`meta.page_title` by `build_page.py`, or you can pass an explicit output dir.

## content.json schema

```jsonc
{
  "meta": {
    "page_title": "...",                       // <title> + browser tab
    "topbar": "Conf · Event 2026",             // yellow top bar
    "hero_label": "Workshop Video Notes",      // small accent label above the H1
    "title_html": "Build Agents That<br>Run for Hours",   // <br> allowed
    "subtitle_en": "...", "subtitle_zh": "...",
    "speakers": "<strong>Name</strong> & <strong>Name</strong> · Role · 1h 15m",
    "source_url": "https://youtu.be/ID", "source_title": "Full talk title — Speakers",
    "slides_desc": "...", "qa_desc": "...", "insights_desc": "...",  // optional; have defaults
    "footer_note": "Event · Notes compiled with Claude Code"
  },
  "slides":   [ { "img": "01.png", "time": "1:00", "title": "EN · 中文",
                  "en": "caption HTML", "zh": "中文说明 HTML" } ],
  "qa":       [ { "n": 1, "who": "Asker · 40:19", "q_en": "...", "q_zh": "...",
                  "a_en": "...", "a_zh": "...", "tag": "Topic" } ],   // [] → whole Q&A section omitted
  "insights": [ { "n": "01", "title_en": "...", "title_zh": "...",
                  "en": ["para", "para"], "zh": ["段", "段"],
                  "source": "Slides 21–22 · Ash 23:45" } ]            // string or ["...","..."]
}
```

Text fields are **trusted HTML** — use inline `<strong>`/`<em>`/`<code>` for emphasis, terms,
and code, exactly as the existing notes do. `slides` alternate left/right automatically.

## Authoring guidance

- **Slides**: title is `English · 中文`; `en`/`zh` are 1–3 sentence explanations of what the
  slide shows and why it matters — not a transcript dump. Bold key terms/numbers.
- **Q&A**: one item per question; `who` = asker (+ affiliation if known) · timestamp; faithful
  bilingual question + answer; short `tag`. Omit the section (`"qa": []`) if the talk has none.
- **Insights**: 5–8 distilled takeaways, each grounded in a `source` (slide range and/or
  speaker + timestamp) so a reader can jump to the moment in the video.

## Context management (改動 F/I — keep context lean)

Heavy work that inflates context must be delegated to sub-agents:
- **Transcript analysis** (step 1): sub-agent reads `transcript.json` and returns a structured
  summary — the full text never enters main context
- **Dedup judgement** (step 2): sub-agent reads the detect table, returns curated manifest
- **Quality review** (step 3b): read the JSON report, not raw image data
- **Content authoring** (step 4): each writing task is a sub-agent that returns one JSON
  fragment — main context only sees the assembled result

Scripts default to compact stdout (counts + flagged items only). Full details go to report
files (`capture_report.json`, `contact_sheet.png`) — read those on demand, not by default.

## Reference

For storyboard-spec parsing, the purple-pixel heuristic, dedup judgement, the login flow,
CDP fallback details, and troubleshooting, read **`references/pipeline.md`** when a step
misbehaves.
