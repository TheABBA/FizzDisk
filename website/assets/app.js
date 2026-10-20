// FizzDisk site — small progressive enhancements
// (no framework; everything is additive so the page stays fine if JS is off)
document.addEventListener('DOMContentLoaded', () => {
  // Smooth-scroll is native via CSS; add active nav highlight.
  const links = document.querySelectorAll('.nav-links a');
  const sections = [...links].map(a => document.querySelector(a.getAttribute('href')));
  const observer = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        links.forEach(l => l.classList.remove('active'));
        const id = '#' + e.target.id;
        const match = [...links].find(l => l.getAttribute('href') === id);
        if (match) match.classList.add('active');
      }
    });
  }, {rootMargin: '-40% 0px -55% 0px'});
  sections.filter(Boolean).forEach(s => observer.observe(s));

  // Download tracking — fire a fire-and-forget beacon on every installer download
  // so the owner can see uptake by version on the private /api/license/stats page.
  // The download link itself is untouched (still points straight at the .exe), so
  // a failed beacon never blocks the download. A disabled button (href="#download")
  // no longer points at the file, so it's not counted while releases are gated.
  //
  // Version auto-detection: fetch download/version.txt (a plain-text file written
  // by the build process alongside the installer) so the tracker always reports
  // the version of the .exe that's actually served — no need to hand-edit
  // config.js on each release.  Falls back to config.VERSION, then 'unknown'.
  let ver = encodeURIComponent(((window.FIZZ_CONFIG || {}).VERSION || 'unknown').toString());
  // Fetch the version first (non-blocking), then attach click handlers.
  // Use sendBeacon (not fetch) for the download ping so it survives the
  // browser navigating to the .exe file (fetch gets cancelled on unload).
  fetch('download/version.txt', {cache: 'no-store'})
    .then(r => r.ok ? r.text() : null)
    .then(t => { if (t) ver = encodeURIComponent(t.trim()); })
    .catch(() => {});
  // Attach the click handler immediately (don't wait for the version fetch)
  // so a fast click still fires the beacon.
  document.querySelectorAll('a[href*="FizzDiskSetup.exe"]').forEach(a => {
    a.addEventListener('click', () => {
      try {
        // Use an image beacon (GET) so the ping survives the browser
        // navigating to the .exe download (fetch/sendBeacon may be cancelled).
        new Image().src = '/api/license/dl?ver=' + ver + '&_t=' + Date.now();
      } catch (e) { /* never block the download */ }
    });
  });
});
