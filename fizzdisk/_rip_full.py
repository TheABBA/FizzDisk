# -*- coding: utf-8 -*-
"""Complete rip of the main UHD title: video + audio + subs (stream copy)."""
import os, time, traceback
from fizzdisk import bdlib

src = "D:\\"
out = r"C:\Users\Abba\Videos\Kung_Fu_Panda_2_UHD.mkv"

titles = bdlib.scan_disc(src, min_seconds=0.0)
main = next((x for x in titles if x.is_main), titles[0] if titles else None)
if not main:
    print("No main title"); raise SystemExit(1)
print("Ripping main title: %s  dur=%.2fs  audio=%d  subs=%d" %
      (main.name, main.duration or 0, len(main.audio), len(main.subtitles)))
print("  -> %s" % out)

t0 = time.time()
progress = {"last": -1.0}
def cb(frac):
    if frac - progress["last"] >= 0.02:
        progress["last"] = frac
        print("  progress %.0f%% (%ds)" % (frac * 100, time.time() - t0))

try:
    rc = bdlib.rip_bd(src, main, out, video=True,
                      audio_idx=list(range(len(main.audio))),
                      sub_idx=list(range(len(main.subtitles))),
                      copy_codecs=True, progress_cb=cb)
    print("rip_bd rc=%d in %.1fs -> size=%d bytes" % (
        rc, time.time() - t0, os.path.getsize(out) if os.path.exists(out) else 0))
except Exception as e:
    print("rip_bd raised:", e)
    traceback.print_exc()
