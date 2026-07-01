#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CDP fallback screenshot capture via web-access proxy.

When Puppeteer/storyboard capture fails (login wall, DRM, bot detection),
use the web-access skill's CDP proxy at http://localhost:3456 to take
compositor-level screenshots and crop the video region with PIL.

Why CDP instead of canvas.drawImage():
  YouTube DRM (Widevine) taints the canvas — drawImage() returns a black
  rectangle. CDP Page.captureScreenshot uses the compositor, bypassing DRM.

Usage:
    capture_cdp.py --video=<URL> --manifest=slide_manifest.json \
                   --out=slides_hd [--cdp=http://localhost:3456] \
                   [--crop=LEFT,TOP,RIGHT,BOTTOM]

The crop box is in PHYSICAL pixels (CSS × DPR). On a DPR=2 Retina display
the default video region was empirically measured at (348, 112, 2524, 1336)
on a 2870×1562 physical-pixel viewport. Pass --crop to override.

Methodology note (Cherny: structured handoffs):
  This script exists as a deterministic fallback — the SKILL.md decision
  tree routes here after a 10-minute timeout on the primary path.
"""
import argparse, base64, json, sys, time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

DEFAULT_CDP = "http://localhost:3456"
DEFAULT_CROP = (348, 112, 2524, 1336)
SETTLE_SECS = 3


def cdp_send(cdp_base, method, params=None):
    targets_url = f"{cdp_base}/json"
    try:
        with urlopen(targets_url, timeout=5) as r:
            targets = json.loads(r.read())
    except URLError as e:
        sys.exit(f"Cannot reach CDP proxy at {cdp_base}: {e}\n"
                 "Ensure web-access skill is running with CDP enabled.")
    ws_url = targets[0].get("webSocketDebuggerUrl", "")
    page_id = targets[0]["id"]
    send_url = f"{cdp_base}/json/protocol"
    payload = json.dumps({"method": method, "params": params or {}}).encode()
    endpoint = f"{cdp_base}/cdp/{page_id}/{method}"
    req = Request(endpoint, data=payload,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception:
        pass
    # Fallback: use /json/protocol style
    return _cdp_http_fallback(cdp_base, page_id, method, params)


def _cdp_http_fallback(cdp_base, page_id, method, params):
    import http.client, urllib.parse
    parsed = urllib.parse.urlparse(cdp_base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
    body = json.dumps({"id": 1, "method": method, "params": params or {}})
    conn.request("POST", f"/json/protocol/{page_id}",
                 body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    return json.loads(resp.read())


def navigate_and_wait(cdp_base, url, wait=5):
    cdp_send(cdp_base, "Page.navigate", {"url": url})
    time.sleep(wait)


def capture_screenshot_png(cdp_base):
    result = cdp_send(cdp_base, "Page.captureScreenshot", {"format": "png"})
    if not result or "data" not in result.get("result", {}):
        data_key = result.get("result", result) if result else {}
        if isinstance(data_key, dict) and "data" in data_key:
            return base64.b64decode(data_key["data"])
        sys.exit("CDP screenshot returned no data. Check proxy connection.")
    return base64.b64decode(result["result"]["data"])


def seek_video(cdp_base, time_s):
    js = f"""
    (function() {{
      const v = document.querySelector('video');
      if (!v) return 'no-video';
      v.currentTime = {time_s};
      return 'seeked';
    }})()
    """
    cdp_send(cdp_base, "Runtime.evaluate", {"expression": js})
    time.sleep(SETTLE_SECS)


def crop_video_region(png_bytes, crop_box):
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png_bytes))
    left, top, right, bottom = crop_box
    if right > img.width or bottom > img.height:
        print(f"Warning: crop box {crop_box} exceeds image {img.size}, adjusting",
              file=sys.stderr)
        right = min(right, img.width)
        bottom = min(bottom, img.height)
    cropped = img.crop((left, top, right, bottom))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser(description="CDP fallback slide capture")
    parser.add_argument("--video", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cdp", default=DEFAULT_CDP)
    parser.add_argument("--crop", default=None,
                        help="Physical-pixel crop: LEFT,TOP,RIGHT,BOTTOM")
    args = parser.parse_args()

    crop_box = DEFAULT_CROP
    if args.crop:
        crop_box = tuple(int(x) for x in args.crop.split(","))
        assert len(crop_box) == 4, "Crop must be LEFT,TOP,RIGHT,BOTTOM"

    manifest = json.loads(Path(args.manifest).read_text())
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"CDP fallback capture: {len(manifest)} slides via {args.cdp}")
    print(f"Crop box (physical px): {crop_box}")

    navigate_and_wait(args.cdp, args.video, wait=8)

    # Dismiss any consent/age-gate overlays
    cdp_send(args.cdp, "Runtime.evaluate", {
        "expression": """
        document.querySelectorAll('[aria-label="Accept all"],' +
          'button.ytp-large-play-button,' +
          'tp-yt-paper-dialog #dismiss-button').forEach(b => b.click());
        """
    })
    time.sleep(2)

    results = []
    for entry in manifest:
        idx = entry["idx"]
        cap_t = entry["cap_t"]
        fname = f"{idx + 1:02d}.png"
        out_file = out_dir / fname

        seek_video(args.cdp, cap_t)
        raw_png = capture_screenshot_png(args.cdp)
        cropped = crop_video_region(raw_png, crop_box)
        out_file.write_bytes(cropped)

        size_kb = len(cropped) / 1024
        results.append({"idx": idx, "file": fname, "cap_t": cap_t,
                        "size_kb": round(size_kb, 1)})
        print(f"  [{idx + 1:02d}] t={cap_t}s → {fname} ({size_kb:.0f} KB)")

    report = {"method": "cdp_fallback", "crop_box": list(crop_box),
              "slides": results}
    report_path = out_dir / "capture_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\nDone. {len(results)} slides → {out_dir}/")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
