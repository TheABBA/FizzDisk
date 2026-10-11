# -*- coding: utf-8 -*-
"""Verify end-to-end: scan_disc on the raw-device-fixed path, then rip the main title."""
import os, time, traceback
from fizzdisk import bdlib

src = "D:\\"

# 1) Scan using the app's own code path
titles = bdlib.scan_disc(src, min_seconds=0.0)
print("scan_disc returned %d title(s):" % len(titles))
for t in titles:
    if t.video:
        vs = "%s@%s" % (t.video.codec_name, t.video.id)
    else:
        vs = "no-video"
    print("  - %s | index=%s main=%s dur=%.2fs playlist=%s clips=%s video=%s audio=%d sub=%d"
          % (t.name, t.index, t.is_main, t.duration or 0,
             (t.info or {}).get("bd_playlist"),
             (t.info or {}).get("bd_clip_count"), vs,
             len(t.audio), len(t.subtitles)))

main = next((x for x in titles if x.is_main), titles[0] if titles else None)
print("\nChosen main title:", main.name if main else "NONE",
      "duration=%.2fs" % (main.duration or 0) if main else "")

# 2) Rip it to a test MKV
if main:
    out = r"C:\Users\Abba\Videos\KFP2_rip_test.mkv"
    t0 = time.time()
    progress = {"last": -1.0}
    def cb(frac):
        if frac - progress["last"] >= 0.02:
            progress["last"] = frac
            print("  progress %.0f%%  (%ds)" % (frac * 100, time.time() - t0))
    try:
        rc = bdlib.rip_bd(src, main, out, progress_cb=cb, copy_codecs=True)
        print("\nrip_bd returned rc=%d in %.1fs -> %s (size=%d bytes)"
              % (rc, time.time() - t0, out, os.path.getsize(out) if os.path.exists(out) else 0))
    except Exception as e:
        print("\nrip_bd raised:", e)
        traceback.print_exc()
