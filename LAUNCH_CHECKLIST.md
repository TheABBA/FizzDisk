# FizzDisk Launch Readiness Checklist

Tracked here so nothing blocks a release. Update the boxes as each step is verified.

## Code signing (Azure Artifact Signing)

- [ ] **Create the certificate profile** — Azure portal → Artifact Signing account
      `fizzdiskcodesign` → **Objects → Certificate profiles → Create**. Type:
      **Public Trust**. Name (e.g. `fizzdiskbasic`), select a validated identity.
- [ ] **Complete identity validation** — `fizzdiskcodesign` → **Objects →
      Identity validations** → **Individual** (recommended) or **Organization**.
      Individual = AU10TIX via portal link (minutes); Organization needs a legal
      entity >3 years old (1–20 business days).
- [ ] **Assign the signer/verifier roles** — `fizzdiskcodesign` → **Access control
      (IAM)** → assign your user both **Artifact Signing Certificate Profile
      Signer** and **Artifact Signing Identity Verifier**.
- [ ] **Create `signing_config.json`** in the repo (gitignored) with:
      `endpoint = https://eus.codesigning.azure.net/`, `account = fizzdiskcodesign`,
      `profile = <certificate profile name>`. (Alternatively set
      `AZURE_SIGNING_ENDPOINT/_ACCOUNT/_PROFILE` env vars.)
- [ ] **Build signed** — `python build.py` must sign the app exe AND the Inno
      installer (log shows `[sign azure]` for both). `sign.exe` is at
      `~/.dotnet/tools/sign.exe`; auth path for the sign tool must be working
      (DefaultAzureCredential: `az login` or `AZURE_TENANT_ID/CLIENT_ID/SECRET`).
- [ ] **Verify the signature** — right-click both the installer and the installed
      `FizzDisk.exe` → Properties → Digital Signatures shows a trusted signature;
      SmartScreen no longer says "unknown publisher".
- [ ] **Deploy the signed installer** and update the site `DOWNLOAD_URL`.

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
