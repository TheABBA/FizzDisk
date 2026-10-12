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
});
