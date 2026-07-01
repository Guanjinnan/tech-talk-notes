#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect which storyboard frames are real full-screen slides (vs. speaker/stage shots).

Consumes meta.json from get_meta.mjs (storyboard sheet-URL template + geometry), fetches
the storyboard sheets, and uses a purple-pixel-fraction heuristic to separate slides
(near-0% purple, bright, mostly white) from camera-on-speaker shots (35–42% purple stage
backdrop). Segments on visual transitions, keeps majority-slide segments, and prints a
signature-diff table so Claude can drop near-duplicates and pick final capture timestamps.

Usage:
    detect_slides.py --meta /path/meta.json --out /path/slide_instances.json

Heuristic thresholds are the proven values from the original pipeline — change with care.

Requires: pip install pillow
"""
import argparse, io, json, urllib.request
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("--meta", required=True)
ap.add_argument("--out", default="/tmp/slide_instances.json")
args = ap.parse_args()

meta = json.load(open(args.meta, encoding="utf-8"))
sb = meta["storyboard"]
TEMPLATE = sb["sheet_url_template"]            # contains {N}
FW, FH = sb["frame_w"], sb["frame_h"]
GRID = sb["cols"] * sb["rows"]                 # frames per full sheet
NSHEETS = sb["nsheets"]
IVL = sb.get("interval_s") or max(1, round(meta["duration"] / max(1, sb["frame_count"])))
print(f"storyboard L{sb['level']} {sb['cols']}x{sb['rows']} {FW}x{FH} @ {IVL}s, {NSHEETS} sheets")

def fetch(n, tries=4):
    url = TEMPLATE.replace("{N}", str(n))
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return Image.open(io.BytesIO(r.read())).convert("RGB")
        except Exception:
            pass
    return None

frames = []
for n in range(NSHEETS):
    sheet = fetch(n)
    if sheet is None:
        print(f"  WARN: sheet M{n} failed after retries; stopping at {n} sheets")
        break
    w, h = sheet.size
    cols, rows = w // FW, h // FH
    for i in range(cols * rows):
        r, c = i // cols, i % cols
        fr = sheet.crop((c * FW, r * FH, c * FW + FW, r * FH + FH))
        t = (n * GRID + i) * IVL
        frames.append((t, fr))
if not frames:
    raise SystemExit("No storyboard frames fetched — check meta.json sheet_url_template.")
print(f"fetched {len(frames)} frames ({frames[-1][0]//60}:{frames[-1][0]%60:02d} max)")

def stats(im):
    px = list(im.getdata()); N = len(px)
    bright = 0.0; white = 0; purple = 0
    for (R, G, B) in px:
        bright += (R + G + B)
        if min(R, G, B) > 175: white += 1
        if R > 70 and B > 70 and G < R * 0.72 and G < B * 0.72: purple += 1
    return bright / (N * 3), white / N, purple / N

def sig(im):  # perceptual signature: 16x9 grayscale
    return list(im.convert("L").resize((16, 9)).getdata())

def sdiff(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)

def fdiff(a, b):
    a = a.resize((32, 18)); b = b.resize((32, 18))
    pa = list(a.getdata()); pb = list(b.getdata())
    return sum(abs(r1-r2)+abs(g1-g2)+abs(b1-b2) for (r1,g1,b1),(r2,g2,b2) in zip(pa, pb)) / (len(pa)*3)

rows = []; prev = None
for t, fr in frames:
    br, wf, pf = stats(fr)
    d = fdiff(prev, fr) if prev is not None else 0
    is_slide = (wf > 0.28 and pf < 0.12 and br > 120)
    rows.append({"t": t, "br": br, "wf": wf, "pf": pf, "d": d, "slide": is_slide, "sig": sig(fr)})
    prev = fr

# segment on transitions
segs = []; cur = [rows[0]]
for r in rows[1:]:
    if r["d"] > 18: segs.append(cur); cur = [r]
    else: cur.append(r)
segs.append(cur)

# slide instances = segments that are majority-slide; representative = cleanest (max white)
instances = []
for s in segs:
    if sum(1 for r in s if r["slide"]) / len(s) >= 0.5:
        rep = max(s, key=lambda r: r["wf"])
        instances.append({"t0": s[0]["t"], "t1": s[-1]["t"], "rep": rep})

mm = lambda x: f"{x//60}:{x%60:02d}"
print(f"\n{len(instances)} raw slide instances. Inter-instance signature diffs:\n")
print(" #  t_rep   range        white  sigDiff_prev")
for i, inst in enumerate(instances):
    sd = sdiff(inst["rep"]["sig"], instances[i-1]["rep"]["sig"]) if i > 0 else 999
    print(f"{i:2} {mm(inst['rep']['t']):>6}  {mm(inst['t0'])}-{mm(inst['t1']):<6}  {inst['rep']['wf']*100:4.0f}%  {sd:6.1f}")

out = [{"idx": i, "t_rep": inst["rep"]["t"], "t0": inst["t0"], "t1": inst["t1"],
        "white": round(inst["rep"]["wf"], 3), "sig": inst["rep"]["sig"]}
       for i, inst in enumerate(instances)]
json.dump(out, open(args.out, "w"), ensure_ascii=False)
print(f"\nsaved {len(out)} instances -> {args.out}")
print("Next: review the table, drop near-duplicate / speaker rows, and write slide_manifest.json")
print('  as [{ "idx": <0-based output index>, "cap_t": <t_rep or hand-picked seconds> }, ...]')
