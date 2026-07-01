#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automated slide quality gate — rubric scoring + contact sheet.

Scores each captured slide PNG on four metrics and flags problems
so the agent reads a report instead of eyeballing 30+ images.

Rubric (Cherny: adversarial evaluator / gradable rubrics):
  - wf: white-pixel fraction (slide content indicator)
  - pf: purple-pixel fraction (stage/speaker indicator)
  - brightness: mean luminance
  - sigDiff: perceptual difference from previous frame

Output:
  - capture_report.json with per-slide scores and flags
  - contact_sheet.png thumbnail grid for quick visual scan

Usage:
    check_slides.py --dir=slides_hd [--out=capture_report.json]
                    [--sheet=contact_sheet.png]
"""
import argparse, json, re, sys
from pathlib import Path

try:
    from PIL import Image
    import numpy as np
except ImportError:
    sys.exit("Requires: pip install pillow numpy")


def is_purple(r, g, b):
    return (r > 70) & (b > 70) & (g < 0.72 * r) & (g < 0.72 * b)


def is_white(r, g, b):
    return (r > 220) & (g > 220) & (b > 220)


def score_image(img_path):
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    total = r.shape[0] * r.shape[1]
    wf = float(np.sum(is_white(r, g, b)) / total)
    pf = float(np.sum(is_purple(r, g, b)) / total)
    brightness = float(np.mean(arr))
    return {"wf": round(wf, 4), "pf": round(pf, 4),
            "brightness": round(brightness, 1),
            "width": img.width, "height": img.height}


def perceptual_diff(path_a, path_b, size=(64, 64)):
    a = np.array(Image.open(path_a).convert("L").resize(size), dtype=np.float32)
    b = np.array(Image.open(path_b).convert("L").resize(size), dtype=np.float32)
    return float(np.mean(np.abs(a - b)))


def classify(scores, min_width=1280):
    flags = []
    # Resolution is the property that actually breaks capture (ABR starts at
    # 144p): assert it directly, don't infer it from content heuristics.
    if scores["width"] < min_width:
        flags.append("low-resolution")
    if scores["pf"] >= 0.12:
        flags.append("suspect-speaker-shot")
    if scores["wf"] < 0.10 and scores["brightness"] < 100:
        flags.append("suspect-dark-frame")
    if scores["wf"] < 0.15 and scores["pf"] >= 0.05:
        flags.append("suspect-stage-wide")
    return flags


def make_contact_sheet(paths, cols=6, thumb_w=320):
    if not paths:
        return None
    thumbs = []
    for p in paths:
        img = Image.open(p).convert("RGB")
        ratio = thumb_w / img.width
        thumb_h = int(img.height * ratio)
        thumbs.append(img.resize((thumb_w, thumb_h), Image.LANCZOS))
    rows_count = (len(thumbs) + cols - 1) // cols
    thumb_h = thumbs[0].height
    sheet = Image.new("RGB", (cols * thumb_w, rows_count * thumb_h), (40, 40, 40))
    for i, th in enumerate(thumbs):
        row, col = divmod(i, cols)
        sheet.paste(th, (col * thumb_w, row * thumb_h))
    return sheet


def main():
    parser = argparse.ArgumentParser(description="Slide quality gate")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--out", default="capture_report.json")
    parser.add_argument("--sheet", default="contact_sheet.png")
    parser.add_argument("--min-width", type=int, default=1280,
                        help="HD floor; captures narrower than this are flagged low-resolution")
    args = parser.parse_args()

    slides_dir = Path(args.dir)
    pngs = sorted(slides_dir.glob("*.png"),
                  key=lambda p: re.sub(r'\D', '', p.stem).zfill(5))
    pngs = [p for p in pngs if p.name not in ("contact_sheet.png", "capture_report.json")]
    if not pngs:
        sys.exit(f"No PNGs found in {slides_dir}")

    results = []
    prev_path = None
    flagged_count = 0
    suspect_dup_count = 0

    for p in pngs:
        scores = score_image(p)
        flags = classify(scores, args.min_width)

        sig_diff = None
        if prev_path:
            sig_diff = round(perceptual_diff(prev_path, p), 2)
            if sig_diff < 3.0:
                flags.append("suspect-duplicate")
                suspect_dup_count += 1
        scores["sigDiff_prev"] = sig_diff

        is_slide = (scores["wf"] > 0.28 and scores["pf"] < 0.12
                    and scores["brightness"] > 120)

        entry = {"file": p.name, **scores, "is_slide": is_slide,
                 "flags": flags}
        results.append(entry)
        if flags:
            flagged_count += 1
        prev_path = p

    # Summary
    total = len(results)
    good = sum(1 for r in results if r["is_slide"] and not r["flags"])
    low_res_count = sum(1 for r in results if "low-resolution" in r["flags"])
    summary = {
        "total": total,
        "good_slides": good,
        "flagged": flagged_count,
        "suspect_duplicates": suspect_dup_count,
        "low_resolution": low_res_count,
        "min_width": args.min_width,
        "min_captured_width": min((r["width"] for r in results), default=0),
        "verdict": "all-clear" if flagged_count == 0 else "needs-review",
    }

    report = {"summary": summary, "slides": results}
    report_path = Path(args.out)
    report_path.write_text(json.dumps(report, indent=2))

    # Contact sheet
    sheet = make_contact_sheet(pngs)
    if sheet:
        sheet_path = Path(args.sheet)
        sheet.save(sheet_path)

    # Compact stdout for agent consumption
    print(f"Quality gate: {total} slides — {good} good, {flagged_count} flagged "
          f"(min width {summary['min_captured_width']}px, floor {args.min_width}px)")
    if flagged_count:
        print("Flagged slides:")
        for r in results:
            if r["flags"]:
                print(f"  {r['file']}: {', '.join(r['flags'])} "
                      f"({r['width']}x{r['height']}, wf={r['wf']}, pf={r['pf']}, "
                      f"sigDiff={r['sigDiff_prev']})")
    print(f"Full report: {report_path}")
    if sheet:
        print(f"Contact sheet: {args.sheet}")


if __name__ == "__main__":
    main()
