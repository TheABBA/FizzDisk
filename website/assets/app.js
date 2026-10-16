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
  const cfg = window.FIZZ_CONFIG || {};
  const ver = encodeURIComponent((cfg.VERSION || 'unknown').toString());
  document.querySelectorAll('a[href*="FizzDiskSetup.exe"]').forEach(a => {
    a.addEventListener('click', () => {
      try {
        fetch('/api/license/dl?ver=' + ver, {keepalive: true});
      } catch (e) { /* never block the download */ }
    });
  });
});
