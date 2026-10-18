// 横向滑动区域：边缘渐隐提示，并把当前项滚到可见处
(() => {
  const SEL = '.chips, .brief-nav-track, .sidenav-links, .segmented, .search-history-items';
  const update = (el) => {
    const over = el.scrollWidth > el.clientWidth + 2;
    el.classList.toggle('fade-end', over && el.scrollLeft + el.clientWidth < el.scrollWidth - 2);
    el.classList.toggle('fade-start', over && el.scrollLeft > 2);
  };
  const center = (el) => {
    const a = el && el.querySelector('[aria-current]');
    if (!a || el.scrollWidth <= el.clientWidth) return;
    const r = a.getBoundingClientRect(), n = el.getBoundingClientRect();
    if (r.left < n.left + 8 || r.right > n.right - 8) el.scrollLeft += r.left - n.left - (n.width - r.width) / 2;
  };
  const all = () => document.querySelectorAll(SEL).forEach((el) => {
    if (!el.dataset.fade) { el.dataset.fade = '1'; el.addEventListener('scroll', () => update(el), { passive: true }); center(el); }
    update(el);
  });
  window.newsCenterActive = (el) => { center(el); update(el); };
  window.addEventListener('resize', all);
  new MutationObserver(all).observe(document.body, { childList: true, subtree: true });
  all();

  const backToTop = document.querySelector('[data-back-to-top]');
  if (backToTop) {
    const updateBackToTop = () => { backToTop.hidden = window.scrollY < 320; };
    window.addEventListener('scroll', updateBackToTop, { passive: true });
    window.addEventListener('pageshow', updateBackToTop);
    backToTop.addEventListener('click', () => {
      window.scrollTo({ top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
    });
    updateBackToTop();
  }
})();
