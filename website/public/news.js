// 新闻列表：分类筛选、每次多显示 12 条；新闻详情：照片看大图（左右切换、Esc、手机滑动）
(() => {
  const grid = document.getElementById('news-grid');
  if (grid) {
    const items = [...grid.querySelectorAll('.news-item')];
    const filters = [...document.querySelectorAll('.news-filter [data-filter]')];
    const more = document.querySelector('.news-more');
    const empty = document.querySelector('.news-filter-empty');
    let category = 'all', shown = 12;
    const matches = () => items.filter(el => category === 'all' || el.dataset.category === category);
    const draw = () => {
      const list = matches();
      items.forEach(el => { el.hidden = true; });
      list.slice(0, shown).forEach(el => { el.hidden = false; });
      if (empty) empty.hidden = list.length !== 0;
      if (more) {
        const rest = Math.max(0, list.length - shown);
        more.hidden = rest === 0;
        more.textContent = more.dataset.moreTemplate.replace('{n}', rest);
        more.setAttribute('aria-expanded', String(rest === 0));
      }
    };
    filters.forEach(btn => btn.addEventListener('click', () => {
      category = btn.dataset.filter;
      shown = 12;
      filters.forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
      draw();
    }));
    if (more) more.addEventListener('click', () => { shown += 12; draw(); });
    draw();
  }

  const photos = [...document.querySelectorAll('.news-photo')];
  if (!photos.length) return;
  const wrap = photos[0].closest('[data-label-close]') || document.querySelector('[data-label-close]') || photos[0];
  const dialog = document.createElement('dialog');
  dialog.className = 'news-lightbox';
  const close = document.createElement('button');
  const prev = document.createElement('button');
  const next = document.createElement('button');
  const img = document.createElement('img');
  close.type = prev.type = next.type = 'button';
  close.className = 'news-lightbox-close';
  prev.className = 'news-lightbox-prev';
  next.className = 'news-lightbox-next';
  close.setAttribute('aria-label', wrap.dataset.labelClose || 'Close');
  prev.setAttribute('aria-label', wrap.dataset.labelPrev || 'Previous');
  next.setAttribute('aria-label', wrap.dataset.labelNext || 'Next');
  close.textContent = '×';
  prev.textContent = '‹';
  next.textContent = '›';
  dialog.append(close, prev, img, next);
  document.body.append(dialog);
  let current = 0;
  const show = i => {
    current = (i + photos.length) % photos.length;
    img.src = photos[current].dataset.full;
    img.alt = photos[current].getAttribute('aria-label') || '';
  };
  photos.forEach((photo, i) => photo.addEventListener('click', () => { show(i); dialog.showModal(); }));
  close.addEventListener('click', () => dialog.close());
  prev.addEventListener('click', event => { event.stopPropagation(); show(current - 1); });
  next.addEventListener('click', event => { event.stopPropagation(); show(current + 1); });
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft') show(current - 1);
    if (event.key === 'ArrowRight') show(current + 1);
  });
  let startX = null;
  dialog.addEventListener('touchstart', event => { startX = event.touches[0].clientX; }, { passive: true });
  dialog.addEventListener('touchend', event => {
    if (startX === null) return;
    const dx = event.changedTouches[0].clientX - startX;
    if (Math.abs(dx) > 40) show(current + (dx < 0 ? 1 : -1));
    startX = null;
  }, { passive: true });
})();
