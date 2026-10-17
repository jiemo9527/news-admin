// 首页实时筛选：输入关键词（停顿 350ms）或切换下拉框后，局部刷新结果，不用点「搜索」。
(() => {
  const form = document.querySelector('[data-live-form]');
  const resultsEl = () => document.querySelector('[data-live="results"]');
  if (!form || !resultsEl() || !window.fetch || !window.DOMParser) return;
  let timer = null;
  let controller = null;
  let composing = false;

  const query = () => {
    const params = new URLSearchParams();
    for (const [key, value] of new FormData(form)) {
      const v = String(value).trim();
      if (v && !(key === 'range' && v === 'all') && !(key === 'sort' && v === 'time')) params.set(key, v);
    }
    const qs = params.toString();
    return '/' + (qs ? '?' + qs : '');
  };

  const load = async (url, push = true) => {
    if (controller) controller.abort();
    controller = new AbortController();
    resultsEl().setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url, { signal: controller.signal, headers: { 'X-Requested-With': 'fetch' } });
      if (response.redirected && new URL(response.url).pathname === '/login') { location.href = response.url; return; }
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const fresh = doc.querySelector('[data-live="results"]');
      const chips = doc.querySelector('[data-live="chips"]');
      for (const label of ['时间范围', '排序']) {
        const next = doc.querySelector(`nav[aria-label="${label}"]`);
        const cur = document.querySelector(`nav[aria-label="${label}"]`);
        if (next && cur) cur.replaceWith(next);
      }
      const sources = doc.querySelector('[data-live="sources"]');
      if (fresh) resultsEl().replaceWith(fresh);
      if (chips) document.querySelector('[data-live="chips"]')?.replaceWith(chips);
      if (sources) {
        const current = document.querySelector('[data-live="sources"]');
        if (current && document.activeElement !== current) current.replaceWith(sources);
      }
      if (push) history.replaceState(null, '', url);
      bind();
    } catch (err) {
      if (err.name !== 'AbortError') location.href = url;
    } finally {
      document.querySelector('[data-live="results"]')?.removeAttribute('aria-busy');
    }
  };

  const schedule = (delay) => {
    clearTimeout(timer);
    timer = setTimeout(() => load(query()), delay);
  };

  form.addEventListener('submit', (e) => { e.preventDefault(); clearTimeout(timer); load(query()); });
  form.addEventListener('compositionstart', () => { composing = true; });
  form.addEventListener('compositionend', () => { composing = false; schedule(350); });
  form.addEventListener('input', (e) => {
    if (e.target.matches('input[type=search]') && !composing) schedule(350);
  });
  form.addEventListener('change', (e) => { if (e.target.matches('select')) schedule(0); });

  // 结果区里的分页、排序、主题等链接也局部刷新
  const bind = () => {
    document.querySelectorAll('a[data-live-link], [data-live="chips"] a').forEach((a) => {
      if (a.dataset.bound) return;
      a.dataset.bound = '1';
      a.addEventListener('click', (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        const url = new URL(a.href);
        for (const name of ['range', 'sort', 'topic']) {
          const field = form.elements[name];
          if (field) field.value = url.searchParams.get(name) || (name === 'range' ? 'all' : name === 'sort' ? 'time' : '');
        }
        load(url.pathname + url.search);
        if (a.closest('.pager')) window.scrollTo({ top: form.offsetTop - 70, behavior: 'smooth' });
      });
    });
  };
  bind();
})();
