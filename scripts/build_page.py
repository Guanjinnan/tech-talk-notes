#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render content.json into the bilingual notes page using assets/template.html.

Usage:
    build_page.py <content.json> [output_dir] [--template path]

When output_dir is omitted, a slug is derived from meta.page_title and the
output goes to <slug>/index.html + <slug>/slides_hd/ (改动 A: Pocock vertical
slices — each talk is a self-contained subfolder, never overwrites another).

content.json schema:
    {
      "meta": {
        "page_title": "...", "topbar": "...", "hero_label": "...",
        "title_html": "Build Agents That<br>Run for Hours",
        "subtitle_en": "...", "subtitle_zh": "...",
        "speakers": "<strong>Name</strong> · Role · 1h 15m",
        "source_url": "https://youtu.be/...", "source_title": "...",
        "slides_desc": "...", "qa_desc": "...", "insights_desc": "...",
        "footer_note": "..."
      },
      "slides":   [ {"img":"01.png","time":"1:00","title":"...","en":"...","zh":"..."} ],
      "qa":       [ {"n":1,"who":"Joan · 40:19","q_en":"...","q_zh":"...",
                     "a_en":"...","a_zh":"...","tag":"..."} ],   # omit section if empty
      "insights": [ {"n":"01","title_en":"...","title_zh":"...",
                     "en":["para",...],"zh":["para",...],"source":"..." | ["...","..."]} ]
    }

Bilingual text fields (title, en, zh, q_*, a_*, etc.) are treated as TRUSTED HTML —
inline tags like <strong>/<em>/<code> are passed through verbatim, not escaped.
"""
import json, re, sys, unicodedata
from pathlib import Path

DEFAULTS = {
    "page_title": "Video Notes",
    "topbar": "Tech Talk · Video Notes",
    "hero_label": "Video Notes",
    "title_html": "Untitled Talk",
    "subtitle_en": "",
    "subtitle_zh": "",
    "speakers": "",
    "source_url": "#",
    "source_title": "Source video",
    "slides_desc": "演讲中展示的所有幻灯片，附中英双语内容注释",
    "qa_desc": "观众问答环节，中英双语整理",
    "insights_desc": "视频的核心观点与方法提炼，附视频来源标注",
    "footer_note": "Notes compiled with Claude Code",
}

def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").replace('"', "'")

def render_slides(slides):
    cards = []
    for i, s in enumerate(slides):
        n = i + 1
        rev = " reverse" if i % 2 == 1 else ""
        cards.append(
f'''<!-- Slide {n:02d} -->
<div class="slide-card{rev}">
  <div class="slide-img-wrap">
    <img src="slides_hd/{s['img']}" alt="{strip_tags(s.get('title',''))}" loading="lazy">
    <span class="slide-timestamp">{s.get('time','')}</span>
  </div>
  <div class="slide-text">
    <div class="slide-num">Slide {n:02d}</div>
    <h3>{s.get('title','')}</h3>
    <p class="en">{s.get('en','')}</p>
    <p class="zh">{s.get('zh','')}</p>
  </div>
</div>''')
    return "\n\n".join(cards)

def render_qa(items):
    out = []
    for q in items:
        tag = f'\n  <span class="qa-tag">{q["tag"]}</span>' if q.get("tag") else ""
        out.append(
f'''<!-- Q{q.get('n','')} -->
<div class="qa-item">
  <div class="qa-meta">
    <div class="qa-number">{q.get('n','')}</div>
    <div class="qa-who">{q.get('who','')}</div>
  </div>
  <div class="qa-question">
    <h4>{q.get('q_en','')}</h4>
    <p class="zh-q">{q.get('q_zh','')}</p>
  </div>
  <div class="qa-answer">
    <p class="en-a">{q.get('a_en','')}</p>
    <p class="zh-a">{q.get('a_zh','')}</p>
  </div>{tag}
</div>''')
    return "\n\n".join(out)

def _paras(items):
    items = items if isinstance(items, list) else [items]
    return "\n".join(
        (f'      <p>{p}</p>' if i == 0 else f'      <p style="margin-top:0.8rem;">{p}</p>')
        for i, p in enumerate(items))

def render_insights(items):
    out = []
    for k, ins in enumerate(items):
        src = ins.get("source", "")
        src_list = src if isinstance(src, list) else [src]
        src_html = " ·\n    ".join(f"<span>{s}</span>" for s in src_list if s)
        source_block = (
            f'\n  <div class="insight-source">\n    {src_html}\n  </div>' if src_html else "")
        out.append(
f'''<!-- Insight {k+1} -->
<div class="insight-card">
  <div class="insight-num">{ins.get('n','')}</div>
  <div class="insight-title">{ins.get('title_en','')}</div>
  <div class="insight-title-zh">{ins.get('title_zh','')}</div>
  <div class="insight-body">
    <div class="en-col">
{_paras(ins.get('en', []))}
    </div>
    <div class="zh-col">
{_paras(ins.get('zh', []))}
    </div>
  </div>{source_block}
</div>''')
    return "\n\n".join(out)

def slugify(text):
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")[:60]


def load_content(path):
    """Load content.json with friendly error on parse failure (改动 E)."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        lines = raw[:e.pos].split("\n") if e.pos else []
        line_no = len(lines)
        col_no = len(lines[-1]) + 1 if lines else 0
        # Show context around the error
        all_lines = raw.split("\n")
        start = max(0, line_no - 3)
        end = min(len(all_lines), line_no + 2)
        context = "\n".join(f"  {'→' if i + 1 == line_no else ' '} {i + 1:4d} │ {all_lines[i]}"
                            for i in range(start, end))
        sys.exit(
            f"JSON parse error in {path} at line {line_no}, col {col_no}:\n"
            f"  {e.args[0]}\n\n{context}\n\n"
            f"Tip: curly/smart quotes “” are fine inside string values, but "
            f"ASCII double-quotes \" must be escaped as \\\" within JSON strings."
        )


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tpl_flag = next((a for a in sys.argv[1:] if a.startswith("--template=")), None)
    if not args:
        sys.exit("usage: build_page.py <content.json> [output_dir] [--template=path]")
    content = load_content(args[0])
    tpl_path = Path(tpl_flag.split("=", 1)[1]) if tpl_flag else \
        Path(__file__).resolve().parent.parent / "assets" / "template.html"

    # 改动 A: slug-based subfolder output, never overwrite another talk
    meta = {**DEFAULTS, **content.get("meta", {})}
    if len(args) > 1:
        out_dir = Path(args[1])
    else:
        slug = slugify(meta.get("page_title", "untitled"))
        out_dir = Path(slug)

    out_file = out_dir / "index.html"
    slides_dst = out_dir / "slides_hd"

    if out_file.exists():
        # Check if it was generated by this script (has our marker)
        existing = out_file.read_text(encoding="utf-8", errors="ignore")
        if "<!--tech-talk-notes-generated-->" not in existing:
            sys.exit(
                f"ABORT: {out_file} already exists and was NOT generated by this script.\n"
                f"Will not overwrite. Use a different output dir or remove it manually."
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    slides_dst.mkdir(parents=True, exist_ok=True)

    # Copy slides into the output subfolder if source exists alongside content.json
    content_dir = Path(args[0]).resolve().parent
    src_slides = content_dir / "slides_hd"
    if src_slides.is_dir() and src_slides.resolve() != slides_dst.resolve():
        import shutil
        for png in sorted(src_slides.glob("*.png")):
            shutil.copy2(png, slides_dst / png.name)

    html = tpl_path.read_text(encoding="utf-8")

    # scalar placeholders
    key_map = {
        "PAGE_TITLE": "page_title", "TOPBAR": "topbar", "HERO_LABEL": "hero_label",
        "TITLE_HTML": "title_html", "SUBTITLE_EN": "subtitle_en", "SUBTITLE_ZH": "subtitle_zh",
        "SPEAKERS": "speakers", "SLIDES_DESC": "slides_desc", "QA_DESC": "qa_desc",
        "INSIGHTS_DESC": "insights_desc", "SOURCE_URL": "source_url",
        "SOURCE_TITLE": "source_title", "FOOTER_NOTE": "footer_note",
    }
    for marker, key in key_map.items():
        html = html.replace("{{" + marker + "}}", str(meta.get(key, "")))

    # content sections
    html = html.replace("<!-- SLIDES -->", render_slides(content.get("slides", [])))
    html = html.replace("<!-- INSIGHTS -->", render_insights(content.get("insights", [])))

    qa = content.get("qa", []) or []
    if qa:
        html = html.replace("<!-- QA_ITEMS -->", render_qa(qa))
        for m in ("<!--QA_SECTION_START-->", "<!--QA_SECTION_END-->",
                  "<!--QA_NAV_START-->", "<!--QA_NAV_END-->"):
            html = html.replace(m, "")
    else:
        html = re.sub(r"<!--QA_SECTION_START-->.*?<!--QA_SECTION_END-->", "", html, flags=re.DOTALL)
        html = re.sub(r"<!--QA_NAV_START-->.*?<!--QA_NAV_END-->", "", html, flags=re.DOTALL)

    # Stamp with generation marker for overwrite protection
    html = html.replace("</head>", "<!--tech-talk-notes-generated-->\n</head>", 1)

    out_file.write_text(html, encoding="utf-8")
    print(f"Wrote {out_file} — {len(content.get('slides', []))} slides, "
          f"{len(qa)} Q&A, {len(content.get('insights', []))} insights")

if __name__ == "__main__":
    main()
