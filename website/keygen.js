// FizzDisk product-key generator, works in any browser context (incl. file://).
// Mirrors fizzdisk/license.py generate_key() exactly:
//   body = 5 random bytes ; sig = HMAC-SHA256(SECRET, body)[:5]
//   raw = body+sig (10 bytes) -> base32 (16 chars, no padding) -> "FZFK-XXXX-..."
// Uses the browser/Node WebCrypto where available, otherwise a built-in
// pure-JS HMAC-SHA256 fallback, so it works from https, localhost AND file://.
(function (global) {
  "use strict";
  var FIZZ_SECRET = "FizzDisk2026-LicKey-UnlimitedBackups-ForLife";

  function b32(bytes) {
    var ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    var bits = 0, val = 0, out = "";
    for (var j = 0; j < bytes.length; j++) {
      val = (val << 8) | bytes[j];
      bits += 8;
      while (bits >= 5) { out += ALPH[(val >> (bits - 5)) & 31]; bits -= 5; }
    }
    if (bits > 0) { out += ALPH[(val << (5 - bits)) & 31]; }
    return out;
  }

  function sha256(bytes) {
    var K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
      0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
      0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
      0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
      0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
      0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
      0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
      0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
    var H = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    var l = bytes.length, bitlen = l * 8;
    var total = l + 1 + (((l % 64) < 56) ? (55 - (l % 64)) : (119 - (l % 64))) + 8;
    var msg = new Uint8Array(total);
    msg.set(bytes, 0);
    msg[l] = 0x80;
    var dv = new DataView(msg.buffer);
    dv.setUint32(total - 8, Math.floor(bitlen / 4294967296), false);
    dv.setUint32(total - 4, bitlen >>> 0, false);
    function rotr(x, n) { return (x >>> n) | (x << (32 - n)); }
    for (var off = 0; off < total; off += 64) {
      var w = new Array(64);
      for (var i = 0; i < 16; i++) { w[i] = dv.getUint32(off + i * 4, false); }
      for (var i = 16; i < 64; i++) {
        var s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        var s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
      for (var i = 0; i < 64; i++) {
        var S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        var ch = (e & f) ^ (~e & g);
        var t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
        var S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + maj) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
      H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
    }
    var out = new Uint8Array(32), odv = new DataView(out.buffer);
    for (var i = 0; i < 8; i++) { odv.setUint32(i * 4, H[i], false); }
    return out;
  }

  function hmacSha256(keyBytes, msg) {
    var BLOCK = 64;
    if (keyBytes.length > BLOCK) keyBytes = sha256(keyBytes);
    var kp = new Uint8Array(BLOCK); kp.set(keyBytes.slice(0, BLOCK));
    var ipad = new Uint8Array(BLOCK), opad = new Uint8Array(BLOCK);
    for (var i = 0; i < BLOCK; i++) { ipad[i] = kp[i] ^ 0x36; opad[i] = kp[i] ^ 0x5c; }
    var inner = new Uint8Array(BLOCK + msg.length);
    inner.set(ipad, 0); inner.set(msg, BLOCK);
    var ih = sha256(inner);
    var outer = new Uint8Array(BLOCK + ih.length);
    outer.set(opad, 0); outer.set(ih, BLOCK);
    return sha256(outer);
  }

  function randBytes(n) {
    var b = new Uint8Array(n);
    if (global.crypto && global.crypto.getRandomValues) { global.crypto.getRandomValues(b); }
    else { for (var i = 0; i < n; i++) { b[i] = Math.floor(Math.random() * 256); } }
    return b;
  }

  function enc(b) { return new TextEncoder().encode(b); }

  function sign(secretStr, data) {
    if (global.crypto && global.crypto.subtle) {
      return global.crypto.subtle.importKey("raw", enc(secretStr),
        { name: "HMAC", hash: "SHA-256" }, false, ["sign"])
        .then(function (key) { return global.crypto.subtle.sign("HMAC", key, data); })
        .then(function (buf) { return new Uint8Array(buf); });
    }
    return Promise.resolve(hmacSha256(enc(secretStr), data));
  }

  function generateFizzKey() {
    var body = randBytes(5);
    return sign(FIZZ_SECRET, body).then(function (sig) {
      var raw = new Uint8Array(10);
      raw.set(body, 0);
      raw.set(sig.slice(0, 5), 5);
      return "FZFK-" + b32(raw).match(/.{1,4}/g).join("-");
    });
  }

  global.FizzKeyGen = { generateFizzKey: generateFizzKey, b32: b32, sha256: sha256, hmacSha256: hmacSha256 };

  // Node test: print one key so the app can validate it.
  if (typeof window === "undefined") { generateFizzKey().then(function (k) { console.log(k); }); }
})(typeof window !== "undefined" ? window : globalThis);
