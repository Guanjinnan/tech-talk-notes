# tech-talk-notes

A [Claude Code](https://claude.com/claude-code) skill that turns a YouTube tech talk into a
bilingual (中文 + English) HTML study-notes page — every slide captured at native resolution
with CN/EN captions, the Q&A organized bilingually, and the core insights synthesized with
in-video timestamps, all in a fixed editorial style with a click-to-zoom lightbox.

## What it produces

A self-contained folder per talk:

```
<talk-slug>/
  index.html      ← the notes page (single file, no build step to view)
  slides_hd/      ← native-resolution slide screenshots
    01.png …
```

Three sections: **Presentation Slides** (each with a screenshot + bilingual caption), **Q&A**
(bilingual, one item per question), and **Core Insights** (5–8 distilled takeaways, each with a
timestamp so you can jump back to the moment in the video).

## How it works

The pipeline splits fragile mechanics (deterministic scripts) from high-value authoring (Claude):

| Stage | Script | Does |
|-------|--------|------|
| Meta + login + transcript | `scripts/get_meta.mjs` | One-time YouTube login via a persistent Chrome profile; extracts native resolution, storyboard spec, and the timestamped transcript |
| Slide detection | `scripts/detect_slides.py` | Finds candidate slide moments from the storyboard |
| Capture | `scripts/capture_slides.mjs` | Forces highest video quality (works around YouTube ABR starting at 144p) and captures each slide at native resolution |
| Quality gate | `scripts/check_slides.py` | Scores every capture on resolution / content / duplication; flags problems so you read a report, not 30 images |
| Assembly | `scripts/build_page.py` | Renders `content.json` into the final page using `assets/template.html` |

Content authoring (bilingual captions, Q&A, insights) is done by Claude via a digest-first,
parallel fan-out pipeline. The full methodology and design rationale live in
[`references/pipeline.md`](references/pipeline.md); the skill's own instructions are in
[`SKILL.md`](SKILL.md).

## Install

Drop this folder into your Claude Code skills directory:

```
git clone <this-repo> ~/.claude/skills/tech-talk-notes
```

Then in Claude Code, give it a YouTube talk URL and ask for video notes — the skill
auto-activates.

### Requirements

- **Node** with `puppeteer` (Chrome channel installed)
- **Python** with `pillow`, `numpy`
- A YouTube login (done once through the persistent Chrome profile on first run)

## Constraints

YouTube only. `yt-dlp` is bot-blocked for these videos and is not used — the pipeline relies on
the logged-in watch-page player and storyboards. See `references/pipeline.md` for the dead ends
already ruled out.

## Methodology

The pipeline's shape draws from two talks: **Pocock** (tracer-bullet validation, vertical
slices, fail-fast) and **Cherny** (gradable rubrics, adversarial quality gates, deterministic
fallbacks).

## License

MIT
