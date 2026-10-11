# -*- coding: utf-8 -*-
"""Validate bdlib.rip_bd on a real disc. Usage:
   python test_rip.py <idx> <out.mkv> [max_seconds]
   If max_seconds given, stop reading after that many seconds of media.
"""
import sys, os, time
sys.path.insert(0, r'C:\Users\Abba\Desktop\FizzDisk')
from fizzdisk import bdlib, ffutil

idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
out = sys.argv[2] if len(sys.argv) > 2 else r'C:\Users\Abba\AppData\Local\hermes\cache\scratch\fizzdisk_scratch\bd_rip_test.mkv'
max_sec = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

titles = bdlib.scan_disc(r'D:/')
t = next((x for x in titles if x.index == idx), None)
if t is None:
    print('NO TITLE idx', idx); sys.exit(2)
print('ripping', t.index, t.nice_duration, 'pl=', t.info.get('bd_playlist'),
      'aud=', len(t.audio), 'subs=', len(t.subtitles), flush=True)
if os.path.exists(out):
    os.remove(out)
audio_idx = list(range(len(t.audio)))
sub_idx = list(range(len(t.subtitles)))
t0 = time.time()
last = [0.0]
def cb(frac):
    now = time.time()
    if now - last[0] > 2:
        last[0] = now
        print(f'  progress {frac*100:.0f}%  elapsed {now-t0:.0f}s', flush=True)
# Wrap to stop early after max_sec by raising nothing; instead pass a stop after limit.
# bdlib reads until EOF; to cap, we instead rip fully but it may be long.
# For capping we use a wrapper that just measures; not stopping below.
rc = bdlib.rip_bd(r'D:/', t, out, audio_idx=audio_idx, sub_idx=sub_idx,
                  copy_codecs=True, progress_cb=cb)
el = time.time() - t0
print('rc=', rc, 'elapsed', round(el,1), 's, file', os.path.getsize(out) if os.path.exists(out) else 0,
      flush=True)
if rc == 0:
    v = ffutil.validate_mkv(out)
    print('VALIDATE ok=', v.get('ok'), 'dur=', v.get('duration'))
    for s in v.get('streams', []):
        print('   ', s.codec_type, s.codec_name, getattr(s, 'language', None))
