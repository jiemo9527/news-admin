// ---------- 文章分析：gpt-5.6-luna 联网检索，结果浮层显示 ----------
// 关闭浮层不会中断请求：分析在后台继续，完成后按钮变为「查看分析」并提示；再次点击直接显示结果。
(() => {
  if (!window.fetch) return;
  const jobs = new Map(); // canonical -> { status: 'pending' | 'done' | 'error', title, data, error }
  let panel = null, owner = null, current = null;
  const mobile = () => window.matchMedia('(max-width: 760px)').matches;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const buttonsFor = (cid) => [...document.querySelectorAll('.news-card[data-canonical] [data-analyze]')]
    .filter((b) => b.closest('.news-card').dataset.canonical === cid);

  const LABELS = { pending: '分析中…', done: '查看分析' };
  const sync = (cid) => {
    const status = jobs.get(cid)?.status;
    for (const b of buttonsFor(cid)) {
      b.textContent = LABELS[status] || '分析';
      b.classList.toggle('is-pending', status === 'pending');
      b.classList.toggle('is-ready', status === 'done');
      b.setAttribute('aria-expanded', String(Boolean(panel) && current === cid));
    }
  };
  const syncAll = () => { for (const cid of jobs.keys()) sync(cid); };

  const place = () => {
    if (!panel) return;
    if (mobile()) { panel.style.left = panel.style.top = ''; return; }
    const w = panel.offsetWidth, h = panel.offsetHeight;
    if (!owner?.isConnected) { panel.style.left = `${window.innerWidth - w - 12}px`; panel.style.top = '72px'; return; }
    const r = owner.getBoundingClientRect();
    const left = Math.min(Math.max(12, r.left), window.innerWidth - w - 12);
    const below = r.bottom + 8, above = r.top - h - 8;
    const top = below + h <= window.innerHeight - 12 || above < 64 ? Math.min(below, window.innerHeight - h - 12) : above;
    panel.style.left = `${left}px`; panel.style.top = `${Math.max(64, top)}px`;
  };

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

  // 浮层内容跟随任务状态
  const show = () => {
    const job = jobs.get(current);
    const body = panel?.querySelector('.insight-body');
    if (!job || !body) return;
    if (job.status === 'done') render(body, job.data);
    else if (job.status === 'error') body.replaceChildren(el('p', 'insight-error', job.error));
    else body.replaceChildren(
      el('p', 'insight-loading', '正在联网检索相关信息与最新进展，通常需要 15–60 秒…'),
      el('p', 'insight-hint', '可以关闭此窗口继续浏览，分析会在后台完成，届时按钮变为「查看分析」。'));
    place();
  };

  const notify = (cid, job) => {
    const toast = el('div', `toast toast-insight toast-${job.status === 'done' ? 'success' : 'error'}`);
    toast.setAttribute('role', 'status');
    const short = job.title.length > 24 ? job.title.slice(0, 24) + '…' : job.title;
    toast.append(el('span', '', job.status === 'done' ? `分析完成：${short}` : `分析失败：${job.error}`));
    if (job.status === 'done') {
      const view = el('button', 'toast-action', '查看'); view.type = 'button';
      view.addEventListener('click', () => { toast.remove(); open(cid, buttonsFor(cid)[0] || null, job.title); });
      toast.append(view);
    }
    document.body.append(toast);
    setTimeout(() => toast.remove(), 8000);
  };

  const start = (cid, title) => {
    const job = { status: 'pending', title };
    jobs.set(cid, job);
    const data = new FormData(); data.set('canonical', cid);
    // 不传 AbortSignal：关闭浮层不取消请求
    fetch('/me/analyze', { method: 'POST', body: data, headers: { 'X-Requested-With': 'fetch' } })
      .then(async (r) => {
        if (r.redirected && new URL(r.url).pathname === '/login') { location.href = r.url; return; }
        const json = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof json.detail === 'string' ? json.detail : `分析失败（HTTP ${r.status}）`);
        Object.assign(job, { status: 'done', data: json });
      })
      .catch((err) => Object.assign(job, { status: 'error', error: err.message || '分析失败，请稍后重试' }))
      .finally(() => {
        if (jobs.get(cid) !== job || job.status === 'pending') return;
        sync(cid);
        if (panel && current === cid) show(); else notify(cid, job);
      });
    sync(cid);
  };

  const close = () => {
    if (!panel) return;
    const cid = current, back = owner;
    panel.remove(); panel = null; owner = null; current = null;
    // 失败的任务关闭后清除，下次点击重新分析
    if (jobs.get(cid)?.status === 'error') jobs.delete(cid);
    sync(cid);
    if (back?.isConnected) back.focus({ preventScroll: true });
  };

  const open = (cid, button, title) => {
    close();
    if (!jobs.has(cid)) start(cid, title);
    current = cid; owner = button;
    panel = el('div', 'insight-pop');
    panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'false');
    const head = el('div', 'insight-head');
    const heading = el('strong', '', '分析：' + title);
    heading.id = 'insight-title'; panel.setAttribute('aria-labelledby', heading.id);
    const x = el('button', 'insight-close', '×'); x.type = 'button'; x.setAttribute('aria-label', '关闭分析');
    x.addEventListener('click', close);
    head.append(heading, x);
    const body = el('div', 'insight-body');
    body.setAttribute('aria-live', 'polite');
    panel.append(head, body);
    document.body.append(panel);
    sync(cid); show(); x.focus({ preventScroll: true });
  };

  document.addEventListener('click', (e) => {
    const button = e.target.closest('[data-analyze]');
    if (button) {
      e.preventDefault();
      const card = button.closest('.news-card');
      const cid = card?.dataset.canonical;
      if (!cid) return;
      if (panel && current === cid) { close(); return; }
      open(cid, button, card.querySelector('.news-title')?.textContent.trim() || '');
      return;
    }
    if (panel && !e.target.closest('.insight-pop, .toast-insight')) close();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && panel) close(); });
  // 首页局部刷新替换卡片后，恢复按钮上的任务状态
  document.addEventListener('news:replaced', syncAll);
  window.addEventListener('resize', place);
  window.addEventListener('scroll', () => {
    if (!panel || !owner || mobile()) return;
    const r = owner.getBoundingClientRect();
    if (!owner.isConnected || r.bottom < 0 || r.top > window.innerHeight) close(); else place();
  }, { passive: true });
})();
