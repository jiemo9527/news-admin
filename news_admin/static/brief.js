// 栏目导航持续可见；锚点滚动后标出当前栏目。
(() => {
  const nav = document.querySelector('[data-brief-nav]');
  if (!nav) return;
  const links = [...nav.querySelectorAll('a[href^="#"]')];
  const sections = links.map(a => document.querySelector(a.hash)).filter(Boolean);
  const activate = (id) => links.forEach(a => {
    if (a.hash === '#' + id) a.setAttribute('aria-current', 'location');
    else a.removeAttribute('aria-current');
  }) || window.newsCenterActive?.(nav.querySelector('.brief-nav-track'));
  const updateOffset = () => {
    const header = document.querySelector('.topbar');
    const h = header?.getBoundingClientRect().height || 56;
    nav.style.top = h + 'px';
    document.documentElement.style.scrollPaddingTop = (h + nav.getBoundingClientRect().height + 12) + 'px';
  };
  window.addEventListener('resize', updateOffset);
  updateOffset();
  links.forEach(a => a.addEventListener('click', () => activate(a.hash.slice(1))));
  let pending = false;
  window.addEventListener('scroll', () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      const bottom = nav.getBoundingClientRect().bottom + 20;
      let current = sections[0];
      for (const section of sections) {
        if (section.getBoundingClientRect().top <= bottom) current = section;
      }
      if (current) activate(current.id);
      pending = false;
    });
  }, { passive: true });
  activate(location.hash.slice(1) || sections[0]?.id);
})();
