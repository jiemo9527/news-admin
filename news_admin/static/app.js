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
      if (v && !(key === 'range' && v === 'all') && !(key === 'sort' && v === 'time') && !(key === 'per' && v === '20')) params.set(key, v);
    }
    const qs = params.toString();
    return '/' + (qs ? '?' + qs : '');
  };

  const load = async (url, push = true, resetScroll = false) => {
    if (controller) controller.abort();
    controller = new AbortController();
    resultsEl().setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url, { signal: controller.signal, headers: { 'X-Requested-With': 'fetch' } });
      if (response.redirected && new URL(response.url).pathname === '/login') { location.href = response.url; return; }
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const fresh = doc.querySelector('[data-live="results"]');
      const chips = doc.querySelector('[data-live="chips"]');
      for (const label of ['时间范围', '阅读状态', '排序']) {
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
      // 吸顶筛选栏的 offsetTop 会随滚动变化；分页应在新结果替换后回到页首。
      if (fresh && resetScroll) window.scrollTo({ top: 0, behavior: 'instant' });
    } catch (err) {
      if (err.name !== 'AbortError') location.href = url;
    } finally {
      document.querySelector('[data-live="results"]')?.removeAttribute('aria-busy');
    }
  };

  const schedule = (delay) => {
    clearTimeout(timer);
    timer = setTimeout(() => { load(query()); rememberSoon(); }, delay);
  };

  // ---------- 关键词历史：停止输入约 1.5 秒后保存；连续输入的前缀由服务端合并 ----------
  const historyBox = document.querySelector('[data-search-history]');
  const historyItems = historyBox?.querySelector('[data-history-items]');
  let historyTimer = null;
  const renderHistory = (items) => {
    if (!historyBox || !historyItems) return;
    historyItems.replaceChildren(...items.map((text) => {
      const wrap = document.createElement('span');
      wrap.className = 'history-chip';
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'chip'; b.dataset.q = text; b.textContent = text;
      const x = document.createElement('button');
      x.type = 'button'; x.className = 'history-remove'; x.dataset.historyRemove = text; x.title = '删除'; x.textContent = '×';
      x.setAttribute('aria-label', `删除搜索记录「${text}」`);
      wrap.append(b, x);
      return wrap;
    }));
    historyBox.hidden = !items.length;
  };
  const post = (url, data) => fetch(url, { method: 'POST', body: data, headers: { 'X-Requested-With': 'fetch' } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r)));
  // 保存与清除串行执行；清除会让之前排队的保存结果失效
  let historyQueue = Promise.resolve();
  let historyGeneration = 0;
  const remember = () => {
    const q = String(form.elements.q.value || '').trim();
    if (!q) return;
    const data = new FormData(); data.set('q', q);
    const generation = historyGeneration;
    historyQueue = historyQueue.then(() => (generation === historyGeneration ? post('/me/search-history', data) : null))
      .then((r) => { if (r && generation === historyGeneration) renderHistory(r.items); }).catch(() => {});
  };
  const rememberSoon = () => { clearTimeout(historyTimer); historyTimer = setTimeout(remember, 1500); };
  historyBox?.addEventListener('click', (e) => {
    const remove = e.target.closest('[data-history-remove]');
    if (remove) {
      // 单条删除：同样取消待保存的关键词，避免刚删的词又被写回
      clearTimeout(historyTimer);
      const generation = ++historyGeneration;
      const data = new FormData(); data.set('q', remove.dataset.historyRemove);
      remove.closest('.history-chip')?.remove();
      if (!historyItems?.children.length) historyBox.hidden = true;
      historyQueue = historyQueue.then(() => post('/me/search-history/remove', data))
        .then((r) => { if (generation === historyGeneration) renderHistory(r.items); }).catch(() => {});
      return;
    }
    const chip = e.target.closest('[data-q]');
    if (chip) {
      form.elements.q.value = chip.dataset.q;
      clearTimeout(timer); load(query()); remember();
      return;
    }
    if (e.target.closest('[data-history-clear]')) {
      // 取消待保存的关键词，并忽略清除前已发出的保存结果，避免清除后又被写回
      clearTimeout(historyTimer);
      const generation = ++historyGeneration;
      historyQueue = historyQueue.then(() => post('/me/search-history/clear', new FormData()))
        .then(() => { if (generation === historyGeneration) renderHistory([]); }).catch(() => {});
    }
  });

  form.addEventListener('submit', (e) => { e.preventDefault(); clearTimeout(timer); load(query()); clearTimeout(historyTimer); remember(); });
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
        for (const name of ['range', 'sort', 'topic', 'unread', 'per']) {
          const field = form.elements[name];
          if (field) field.value = url.searchParams.get(name) || (name === 'range' ? 'all' : name === 'sort' ? 'time' : name === 'per' ? field.value : '');
        }
        load(url.pathname + url.search, true, Boolean(a.closest('.pager')));
      });
    });
  };
  bind();
})();

// ---------- 卡片：已读/未读、收藏；长按将该条及以上标为已读 ----------
(() => {
  if (!window.fetch) return;
  const post = (url, data) => fetch(url, { method: 'POST', body: data, headers: { 'X-Requested-With': 'fetch' } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r)));
  const notify = (text, kind = 'success') => {
    const el = document.createElement('div');
    el.className = `toast toast-${kind}`;
    el.setAttribute('role', 'status');
    el.textContent = text;
    document.body.append(el);
    setTimeout(() => el.remove(), 4000);
  };

  const setRead = (card, read) => {
    card.classList.toggle('is-read', read);
    card.classList.toggle('is-unread', !read);
    const dot = card.querySelector('.unread-dot');
    if (read) dot?.remove();
    else if (!dot) {
      const d = document.createElement('span');
      d.className = 'unread-dot'; d.title = '未读'; d.setAttribute('aria-label', '未读');
      card.querySelector('.news-kicker')?.prepend(d);
    }
    const form = card.querySelector('form[data-card-action="read"]');
    const button = form?.querySelector('button');
    if (!form || !button || button.hasAttribute('data-auto-read')) return;
    form.elements.value.value = read ? '0' : '1';
    button.textContent = read ? '标为未读' : '标为已读';
    button.setAttribute('aria-pressed', String(read));
  };

  document.addEventListener('submit', (e) => {
    const form = e.target.closest('form[data-card-action]');
    if (!form) return;
    e.preventDefault();
    const card = form.closest('.news-card');
    const value = form.elements.value.value === '1';
    const button = form.querySelector('button');
    button.disabled = true;
    post(form.action, new FormData(form)).then(() => {
      if (form.dataset.cardAction === 'read') setRead(card, value);
      else {
        form.elements.value.value = value ? '0' : '1';
        button.textContent = value ? '已收藏' : '收藏';
        button.setAttribute('aria-pressed', String(value));
      }
    }).catch(() => notify('操作失败，请刷新后重试', 'error')).finally(() => { if (!button.hasAttribute('data-auto-read')) button.disabled = false; });
  });

  // 简报页「全部标为已读」
  document.querySelector('form[data-mark-all]')?.addEventListener('submit', (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    post(form.action, new FormData()).then((r) => notify(`已将本期 ${r.count} 条标为已读`)).catch(() => form.submit());
  });

  // 长按（手机 / 鼠标按住 600ms）：当前排序下该条及以上全部标为已读（跨页，以页面打开时的数据为准）
  const HOLD_MS = 600;
  let holdTimer = null, start = null, held = false;
  const cancel = () => { clearTimeout(holdTimer); holdTimer = null; document.querySelector('.news-card.is-holding')?.classList.remove('is-holding'); };
  const markUpto = (card) => {
    const list = card.closest('[data-upto]');
    if (!list || !confirm('将这条及其上方的新闻（包括前面的页）全部标为已读？')) return;
    const data = new FormData();
    data.set('canonical', card.dataset.canonical);
    for (const key of ['maxRun', 'at', 'range', 'q', 'sort', 'topic', 'source', 'unread']) {
      data.set(key === 'maxRun' ? 'max_run' : key, list.dataset[key] || '');
    }
    post('/me/read/upto', data).then((r) => {
      for (const c of list.querySelectorAll('.news-card')) {
        setRead(c, true);
        if (c === card) break;
      }
      notify(`已将 ${r.count} 条标为已读`);
    }).catch(() => notify('操作失败，请刷新后重试', 'error'));
  };
  document.addEventListener('pointerdown', (e) => {
    const card = e.target.closest('[data-upto] .news-card');
    if (!card || e.button > 0 || e.target.closest('a, button, summary, input, select, details[open] .news-more-body')) return;
    held = false;
    start = { x: e.clientX, y: e.clientY };
    card.classList.add('is-holding');
    holdTimer = setTimeout(() => { held = true; cancel(); navigator.vibrate?.(20); markUpto(card); }, HOLD_MS);
  });
  document.addEventListener('pointermove', (e) => {
    if (holdTimer && start && Math.hypot(e.clientX - start.x, e.clientY - start.y) > 10) cancel();
  });
  for (const type of ['pointerup', 'pointercancel', 'scroll']) document.addEventListener(type, cancel, { passive: true });
  // 长按后松手不触发点击、文字选择或系统菜单
  document.addEventListener('click', (e) => { if (held) { e.preventDefault(); e.stopPropagation(); held = false; } }, true);
  document.addEventListener('contextmenu', (e) => { if (e.target.closest('[data-upto] .news-card') && (holdTimer || held)) e.preventDefault(); });
})();

// ---------- 文章分析：gpt-5.6-luna 联网检索，结果浮层显示 ----------
(() => {
  if (!window.fetch) return;
  let panel = null, owner = null, controller = null;
  const close = () => {
    controller?.abort(); controller = null;
    panel?.remove(); panel = null;
    owner?.setAttribute('aria-expanded', 'false'); owner?.focus({ preventScroll: true }); owner = null;
  };
  const place = () => {
    if (!panel || !owner) return;
    if (window.matchMedia('(max-width: 760px)').matches) { panel.style.left = panel.style.top = ''; return; }
    const r = owner.getBoundingClientRect(), w = panel.offsetWidth, h = panel.offsetHeight;
    const left = Math.min(Math.max(12, r.left), window.innerWidth - w - 12);
    const below = r.bottom + 8, above = r.top - h - 8;
    const top = below + h <= window.innerHeight - 12 || above < 64 ? Math.min(below, window.innerHeight - h - 12) : above;
    panel.style.left = `${left}px`; panel.style.top = `${Math.max(64, top)}px`;
  };
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const render = (body, data) => {
    body.replaceChildren();
    for (const block of String(data.text).split(/\n(?=【)/)) {
      const m = block.match(/^【([^】]{1,12})】\s*([\s\S]*)$/);
      const section = el('section', 'insight-section');
      if (m) section.append(el('h3', '', m[1]));
      for (const line of (m ? m[2] : block).split('\n').map((s) => s.trim()).filter(Boolean)) section.append(el('p', '', line.replace(/^[-•·]\s*/, '')));
      body.append(section);
    }
    if (data.sources?.length) {
      const refs = el('section', 'insight-section insight-sources');
      refs.append(el('h3', '', '检索来源'));
      const list = el('ol');
      for (const s of data.sources) {
        const a = el('a', '', s.title || s.url);
        a.href = s.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.title = s.url;
        const li = el('li'); li.append(a); list.append(li);
      }
      refs.append(list); body.append(refs);
    }
    const when = data.created_at ? data.created_at.slice(5, 16).replace('T', ' ') : '';
    const note = `${data.model}${data.searched ? ' · 已联网检索' : ' · 未执行联网检索'}${when ? ` · ${when}` : ''}${data.cached ? ' · 缓存' : ''}`;
    body.append(el('p', 'insight-meta', `${note}。AI 生成内容，可能有误，请以原始来源为准。`));
  };
  const open = async (button) => {
    const card = button.closest('.news-card');
    if (owner === button) { close(); return; }
    close();
    owner = button; button.setAttribute('aria-expanded', 'true');
    panel = el('div', 'insight-pop');
    panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'false');
    const head = el('div', 'insight-head');
    const title = el('strong', '', '分析：' + (card.querySelector('.news-title')?.textContent.trim() || ''));
    title.id = 'insight-title'; panel.setAttribute('aria-labelledby', title.id);
    const x = el('button', 'insight-close', '×'); x.type = 'button'; x.setAttribute('aria-label', '关闭分析');
    x.addEventListener('click', close);
    head.append(title, x);
    const body = el('div', 'insight-body');
    body.append(el('p', 'insight-loading', '正在联网检索相关信息与最新进展，通常需要 15–60 秒…'));
    body.setAttribute('aria-live', 'polite');
    panel.append(head, body);
    document.body.append(panel);
    place(); x.focus({ preventScroll: true });
    const mine = controller = new AbortController();
    const data = new FormData(); data.set('canonical', card.dataset.canonical);
    try {
      const r = await fetch('/me/analyze', { method: 'POST', body: data, signal: mine.signal, headers: { 'X-Requested-With': 'fetch' } });
      if (r.redirected && new URL(r.url).pathname === '/login') { location.href = r.url; return; }
      const json = await r.json().catch(() => ({}));
      if (controller !== mine || !panel) return;
      if (!r.ok) throw new Error(typeof json.detail === 'string' ? json.detail : `分析失败（HTTP ${r.status}）`);
      render(body, json);
    } catch (err) {
      if (err.name === 'AbortError' || controller !== mine || !panel) return;
      body.replaceChildren(el('p', 'insight-error', err.message || '分析失败，请稍后重试'));
    }
    place();
  };
  document.addEventListener('click', (e) => {
    const button = e.target.closest('[data-analyze]');
    if (button) { e.preventDefault(); open(button); return; }
    if (panel && !e.target.closest('.insight-pop')) close();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && panel) close(); });
  window.addEventListener('resize', place);
  window.addEventListener('scroll', () => { if (panel && owner && !window.matchMedia('(max-width: 760px)').matches) {
    const r = owner.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight) close(); else place();
  } }, { passive: true });
})();

// ---------- 自动消失的提示气泡（24 小时无新内容警告等） ----------
document.querySelectorAll('[data-autohide]').forEach((el) => {
  setTimeout(() => el.remove(), Number(el.dataset.autohide) || 5000);
});
