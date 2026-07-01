#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect which storyboard frames are real full-screen slides (vs. speaker/stage shots).

Consumes meta.json from get_meta.mjs (storyboard sheet-URL template + geometry), fetches
the storyboard sheets, and classifies frames as slide vs. non-slide using one of two paths:

  - Light theme (white-background PPT): white-pixel fraction + brightness + low purple
  - Dark theme (black-background code talks): text fraction + color variance + low purple

The theme is auto-detected from the frame population. Segments on visual transitions, keeps
majority-slide segments, and prints a signature-diff table so Claude can drop near-duplicates
and pick final capture timestamps.

Usage:
    detect_slides.py --meta /path/meta.json --out /path/slide_instances.json

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
    br = bright / (N * 3)
    wf = white / N
    pf = purple / N

    # Dark-slide signals
    gpx = list(im.convert("L").getdata())
    tf = sum(1 for p in gpx if p > 150) / len(gpx)

    small = im.resize((16, 9))
    spx = list(small.getdata()); sN = len(spx)
    mr = sum(p[0] for p in spx) / sN
    mg = sum(p[1] for p in spx) / sN
    mb = sum(p[2] for p in spx) / sN
    var = sum((p[0]-mr)**2 + (p[1]-mg)**2 + (p[2]-mb)**2 for p in spx) / (sN * 3)

    return br, wf, pf, tf, var

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
    br, wf, pf, tf, var = stats(fr)
    d = fdiff(prev, fr) if prev is not None else 0
    rows.append({"t": t, "br": br, "wf": wf, "pf": pf, "tf": tf, "var": var,
                 "d": d, "sig": sig(fr)})
    prev = fr

# Auto-detect theme from frame population
light_count = sum(1 for r in rows if r["wf"] > 0.28 and r["pf"] < 0.12 and r["br"] > 120)
dark_count = sum(1 for r in rows if r["var"] > 1500 and r["tf"] > 0.10 and r["pf"] < 0.12)

if light_count >= len(rows) * 0.10:
    theme = "light"
elif dark_count >= len(rows) * 0.15:
    theme = "dark"
else:
    theme = "light"

print(f"theme: {theme} (light_hits={light_count}, dark_hits={dark_count}, total={len(rows)})")

# Classify with theme-specific heuristic
for r in rows:
    if theme == "light":
        r["slide"] = (r["wf"] > 0.28 and r["pf"] < 0.12 and r["br"] > 120)
    else:
        r["slide"] = (r["var"] > 1500 and r["tf"] > 0.10 and r["pf"] < 0.12)

# Segment on transitions (adaptive threshold)
FDIFF_TH = 10 if theme == "dark" else 18
segs = []; cur = [rows[0]]
for r in rows[1:]:
    if r["d"] > FDIFF_TH: segs.append(cur); cur = [r]
    else: cur.append(r)
segs.append(cur)

# Slide instances = segments that are majority-slide
instances = []
for s in segs:
    if sum(1 for r in s if r["slide"]) / len(s) >= 0.5:
        if theme == "dark":
            rep = max(s, key=lambda r: r["tf"])
        else:
            rep = max(s, key=lambda r: r["wf"])
        instances.append({"t0": s[0]["t"], "t1": s[-1]["t"], "rep": rep})

mm = lambda x: f"{x//60}:{x%60:02d}"
print(f"\n{len(instances)} raw slide instances (theme={theme}, fdiff_th={FDIFF_TH}). "
      f"Inter-instance signature diffs:\n")
print(" #  t_rep   range        white  text   var    sigDiff_prev")
for i, inst in enumerate(instances):
    sd = sdiff(inst["rep"]["sig"], instances[i-1]["rep"]["sig"]) if i > 0 else 999
    r = inst["rep"]
    print(f"{i:2} {mm(r['t']):>6}  {mm(inst['t0'])}-{mm(inst['t1']):<6}  "
          f"{r['wf']*100:4.0f}%  {r['tf']*100:4.0f}%  {r['var']:5.0f}  {sd:6.1f}")

out = [{"idx": i, "t_rep": inst["rep"]["t"], "t0": inst["t0"], "t1": inst["t1"],
        "white": round(inst["rep"]["wf"], 3),
        "text": round(inst["rep"]["tf"], 3),
        "sig": inst["rep"]["sig"]}
       for i, inst in enumerate(instances)]
json.dump(out, open(args.out, "w"), ensure_ascii=False)
print(f"\nsaved {len(out)} instances -> {args.out}")
print("Next: review the table, drop near-duplicate / speaker rows, and write slide_manifest.json")
print('  as [{ "idx": <0-based output index>, "cap_t": <t_rep or hand-picked seconds> }, ...]')
