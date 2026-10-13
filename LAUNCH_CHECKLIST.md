# FizzDisk Launch Readiness Checklist

Tracked here so nothing blocks a release. Update the boxes as each step is verified.

## Code signing (commercial certificate, not Azure — no monthly fee)

Decision: **Ice Monger, LLC** (>3 yrs) owns `fizzdisk.com`. Get a cheap one-off
**commercial OV code-signing cert** (e.g. **Certum** ~$30–50/yr) issued to
**Ice Monger, LLC** — publisher is a company name, NOT the user's personal name.
(Skip Azure Artifact Signing at $9.99/mo ≈ $120/yr; it remains an optional
fallback via `signing_config.json` + the `sign` tool.)

- [ ] **Buy the OV code-signing cert** — order from Certum (or Sectigo/SSL.com)
      as **Ice Monger, LLC**. Provide business registration details + control of
      `fizzdisk.com` + a matching email. (Optional: a DBA "Ice Monger, LLC d/b/a
      FizzDisk" makes the publisher print "FizzDisk" if desired.)
- [ ] **Place the `.pfx`** in the repo root as `signing_cert.pfx`; set its password
      via `FIZZDISK_CERT_PASS` (or `FIZZDISK_CERT` env var to a custom path).
      `build.py`'s `find_cert()` already auto-detects it.
- [ ] **Make sure a signer exists** — install signtool (Windows SDK) OR use the
      .NET `sign` tool for `code` signing with the pfx. `build.py` falls through
      to the signtool path when no Azure `signing_config.json` is present.
- [ ] **Build signed** — `python build.py` must sign the app exe AND the Inno
      installer (log shows `[sign]` for both), publisher = **Ice Monger, LLC**.
- [ ] **Verify the signature** — right-click both the installer and the installed
      `FizzDisk.exe` → Properties → Digital Signatures shows a trusted signature;
      SmartScreen no longer says "unknown publisher".
- [ ] **Deploy the signed installer** and update the site `DOWNLOAD_URL`; verify
      live Content-Length equals the local byte size.

## Site config (website/assets/config.js) — edit before go-live

- [ ] `BUY_URL` — 2Checkout checkout link (or similar).
- [ ] `PRICE`, `VERSION`, `DOWNLOAD_URL`, `SUCCESS_URL` all final.
- [ ] Download page serves the signed installer; verify live Content-Length equals
      the local byte size.

## Product / licensing

- [ ] `fizzdisk/license.py` `_SECRET` still byte-synced with `website/assets/keygen.js`
      `FIZZ_SECRET` (a mismatch invalidates every issued key).
- [ ] One-device activation API deployed and working (activate/deactivate).
- [ ] 50-backup trial path confirmed.
- [ ] Bundle contents verified: `bin/` has ffmpeg, ffprobe, cd-paranoia, libaacs,
      libbdplus, bluray, vcruntime, libcdio*, keydb.cfg; exe icon is the brand mark.

## Quality gates (revisit on the fresh-install regressions)

- [ ] Movie title search: on-disc metadata (`BDMV/META`) takes precedence over a
      generic/junk volume label; a label that contains the title must resolve.
- [ ] Obfuscated-disc (Lionsgate) full-length verification: the chosen title's
      duration is checked against the known movie runtime and the closest title
      is used. Runtime is fetched from TMDb when a key is set, otherwise a free
      no-key Wikipedia infobox fallback is used (`movie.movie_runtime`).
- [ ] GUI windows all show the FizzDisk brand icon (no Tk feather fallback).
- [ ] Pre-install messaging in place (while the cert is pending): a note on the
      website download page AND pinned on the Inno Welcome page covering the
      SmartScreen "unknown publisher" → More info → Run anyway, and the
      downloaded-file "Unblock" step. MakeMKV was verified as fully unsigned, so
      this is already better than the tool FizzDisk replaces.
