// 新闻动态页面：/{lang}/news/（列表）和 /{lang}/news/{id}/（详情），以及首页“最新动态”
// 列表照 AI 课程列表页的卡片，详情照应用案例详情页（左右两栏 + 右侧信息卡）；外框都用“关于我们”页（和案例页一样，页尾是咨询区）。
// 内容来自后台发布快照，每次请求读取。
const fs = require('fs'), path = require('path');
const frontData = require('./前台数据.cjs');
const { esc, crumbs } = require('./v2-parts.cjs');

const L = {
  zh: {
    label: '新闻动态', intro: '公司动态、课程活动与合作进展，按时间更新。', all: '全部',
    readMore: '阅读全文', videoBadge: '视频', more: '查看更多（还有 {n} 条）', noItems: '暂无内容',
    galleryTitle: '现场照片', viewOriginal: '查看原文', watchVideo: '观看视频',
    prev: '上一条', next: '下一条', allNews: '查看全部动态', close: '关闭',
    prevImage: '上一张', nextImage: '下一张', notFound: '没有找到这条新闻',
    infoTitle: '本篇信息', catLabel: '分类', dateLabel: '日期', videoLabel: '视频', consult: '预约交流',
    videoReplay: '回看现场视频', videoNote: '现场记录',
    ctaTitle: '关注企业 AI 落地？', ctaText: '了解培训、Agent 部署与产学研合作', allNewsKicker: '返回新闻中心',
  },
  vi: {
    label: 'Tin tức', intro: 'Tin công ty, khóa học, sự kiện và hợp tác, cập nhật theo thời gian.', all: 'Tất cả',
    readMore: 'Đọc tiếp', videoBadge: 'Video', more: 'Xem thêm (còn {n} tin)', noItems: 'Chưa có nội dung',
    galleryTitle: 'Hình ảnh tại sự kiện', viewOriginal: 'Xem bài gốc', watchVideo: 'Xem video',
    prev: 'Tin trước', next: 'Tin tiếp theo', allNews: 'Xem tất cả tin tức', close: 'Đóng',
    prevImage: 'Ảnh trước', nextImage: 'Ảnh tiếp theo', notFound: 'Không tìm thấy tin này',
    infoTitle: 'Thông tin bài viết', catLabel: 'Chuyên mục', dateLabel: 'Ngày đăng', videoLabel: 'Video', consult: 'Đặt lịch trao đổi',
    videoReplay: 'Xem lại video sự kiện', videoNote: 'Video tại sự kiện',
    ctaTitle: 'Bạn quan tâm đến ứng dụng AI cho doanh nghiệp?', ctaText: 'Tìm hiểu về đào tạo, triển khai Agent và hợp tác nghiên cứu ứng dụng', allNewsKicker: 'Quay lại trung tâm tin tức',
  },
  en: {
    label: 'News', intro: 'Company news, courses, events and partnerships, updated by date.', all: 'All',
    readMore: 'Read more', videoBadge: 'Video', more: 'View more ({n} remaining)', noItems: 'No content yet',
    galleryTitle: 'Event photos', viewOriginal: 'View original', watchVideo: 'Watch video',
    prev: 'Previous', next: 'Next', allNews: 'View all news', close: 'Close',
    prevImage: 'Previous photo', nextImage: 'Next photo', notFound: 'News item not found',
    infoTitle: 'About this news', catLabel: 'Category', dateLabel: 'Date', videoLabel: 'Video', consult: 'Book a consultation',
    videoReplay: 'Replay event video', videoNote: 'Event recording',
    ctaTitle: 'Bringing AI into your business?', ctaText: 'Explore training, Agent deployment and industry–research partnerships', allNewsKicker: 'Back to News',
  },
};

// 首页“最新动态”的编号由 placeHome() 按实际位置重排，这里写的是放在公司介绍之后时的编号
const HOME = {
  zh: { eyebrow: '02 / 最新动态', title: '我们最近在做的事', intro: '公司动态、课程活动与合作进展，按时间更新。', more: '查看全部动态' },
  vi: { eyebrow: '02 / Tin mới', title: 'Hoạt động gần đây', intro: 'Tin công ty, khóa học, sự kiện và hợp tác, cập nhật theo thời gian.', more: 'Xem tất cả tin tức' },
  en: { eyebrow: '02 / LATEST NEWS', title: 'What we have been working on', intro: 'Company news, courses, events and partnerships, updated by date.', more: 'View all news' },
};

const SITE = 'https://www.evoldream.tech';
const LANGS = ['zh', 'vi', 'en'];

function dateText(lang, date) {
  const m = String(date || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return '';
  const [, y, mo, d] = m;
  if (lang === 'zh') return `${y}.${mo}.${d}`;
  if (lang === 'vi') return `${d}/${mo}/${y}`;
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${Number(d)} ${months[Number(mo) - 1]} ${y}`;
}

function durationText(value) {
  let n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return '--:--';
  n = Math.floor(n);
  return Math.floor(n / 60) + ':' + String(n % 60).padStart(2, '0');
}

function linkText(lang, link) {
  return /youtube\.com|youtu\.be|facebook\.com|tiktok\.com/i.test(link) ? L[lang].watchVideo : L[lang].viewOriginal;
}

// 分类和日期：首页、列表、详情都用这一种写法（分类在前，日期在后）
function metaLine(lang, n) {
  return `<span class="news-meta">${n.category ? `<b class="news-meta-cat">${esc(n.category)}</b>` : ''}<time datetime="${esc(n.date)}">${esc(dateText(lang, n.date))}</time></span>`;
}

function setMeta(html, meta) {
  html = html.replace(/<title>[\s\S]*?<\/title>/, `<title>${esc(meta.title)}</title>`);
  html = html.replace(/<meta name="description" content="[^"]*">/, `<meta name="description" content="${esc(meta.desc)}">`);
  html = html.replace(/<link rel="canonical" href="[^"]*">/, `<link rel="canonical" href="${esc(meta.canonical)}">`);
  html = html.replace(/<meta property="og:title" content="[^"]*">/, `<meta property="og:title" content="${esc(meta.title)}">`);
  html = html.replace(/<meta property="og:description" content="[^"]*">/, `<meta property="og:description" content="${esc(meta.desc)}">`);
  html = html.replace(/<meta property="og:url" content="[^"]*">/, `<meta property="og:url" content="${esc(meta.canonical)}">`);
  if (meta.image) html = html.replace(/<meta property="og:image" content="[^"]*">/, `<meta property="og:image" content="${esc(meta.image)}">`);
  for (const l of LANGS) {
    const hl = l === 'zh' ? 'zh-CN' : l;
    html = html.replace(new RegExp(`(<link rel="alternate" hreflang="${hl}" href=")[^"]*(">)`), `$1${esc(meta.altUrls[l])}$2`);
  }
  html = html.replace(/(<link rel="alternate" hreflang="x-default" href=")[^"]*(">)/, `$1${esc(meta.altUrls.vi)}$2`);
  return html;
}

// 页头的语言切换链接指向新闻页；不碰品牌 logo 等其它 /{lang}/ 链接
function setLangLinks(html, hrefs) {
  for (const l of LANGS) {
    const hl = l === 'zh' ? 'zh-CN' : l;
    html = html.replace(new RegExp(`href="[^"]*"( lang="${hl}")`), `href="${esc(hrefs[l])}"$1`);
  }
  return html;
}

// 列表页和详情页都用“关于我们”页面作外框（和案例页一样），保留页尾咨询区。
// main 只放到页尾咨询区前面，所以不带 </main>（外框里咨询区后面本来就有 </main>）。
function shellAbout(lang, publicRoot, main, meta, langHrefs, { css = ['/case-page.css'], player = false } = {}) {
  let html = fs.readFileSync(path.join(publicRoot, lang, 'about', 'index.html'), 'utf8');
  html = setMeta(html, meta);
  html = setLangLinks(html, langHrefs);
  html = html.replace(/ class="active" aria-current="page"/g, '');
  const i = html.indexOf('<main id="main">'), j = html.indexOf('<section class="contact-section"');
  html = html.slice(0, i) + main.replace(/<\/main>\s*$/, '') + html.slice(j);
  const links = [...css, '/news.css'].map(href => `<link rel="stylesheet" href="${href}">`).join('');
  html = html.replace('</head>', `${links}<script src="/news.js" defer></script></head>`);
  return player ? html.replace('</body>', '<script src="/course-preview.js" defer></script></body>') : html;
}

// Preview rendering rewrites thumbnail URLs to the authenticated original.
function coverThumbnail(url) {
  return url.startsWith('/admin-media/') ? url.replace('/admin-media/', '/admin-thumb/') : url;
}

function card(lang, n, index) {
  const t = L[lang];
  return `<li class="news-item" data-category="${esc(n.categoryId)}" data-index="${index}"><a class="v2-course news-card" href="/${lang}/news/${encodeURIComponent(n.id)}/">`
    + `<span class="v2-course-cover"><img src="${esc(coverThumbnail(n.cover))}" alt="${esc(n.title)}" loading="lazy" decoding="async" width="1600" height="900">`
    + (n.video ? `<span class="news-video-badge" aria-hidden="true">▶ ${esc(t.videoBadge)}</span>` : '') + '</span>'
    + `<span class="v2-course-body">${metaLine(lang, n)}`
    + `<h3>${esc(n.title)}</h3><p class="v2-course-sum">${esc(n.summary)}</p>`
    + `<span class="v2-course-foot"><span></span><span class="text-link">${esc(t.readMore)} →</span></span></span></a></li>`;
}

function listMain(lang, items) {
  const t = L[lang];
  const cats = [...new Map(items.filter(n => n.categoryId && n.category).map(n => [n.categoryId, n.category])).entries()];
  const filter = cats.length > 1
    ? `<div class="filter-bar news-filter" role="group" aria-label="${esc(t.label)}"><button type="button" data-filter="all" aria-pressed="true">${esc(t.all)}</button>${cats.map(([id, name]) => `<button type="button" data-filter="${esc(id)}" aria-pressed="false">${esc(name)}</button>`).join('')}</div>`
    : '';
  const rest = Math.max(0, items.length - 12);
  const more = rest > 0
    ? `<button class="v2-more news-more" type="button" aria-expanded="false" data-more-template="${esc(t.more)}">${esc(t.more.replace('{n}', rest))}</button>`
    : '';
  const body = items.length
    ? `<ul class="v2-course-grid news-grid" id="news-grid" role="list">${items.map((n, i) => card(lang, n, i)).join('')}</ul>${more}`
    : `<p class="v2-empty">${esc(t.noItems)}</p>`;
  return `<main id="main"><section class="page-hero news-list-hero"><div class="container">${crumbs(lang, [[t.label]], `/${lang}/#news`)}<h1>${esc(t.label)}</h1><p class="intro">${esc(t.intro)}</p></div></section>`
    + `<section class="v2-courses news-list"><div class="container">${filter}${body}<p class="v2-empty news-filter-empty" hidden>${esc(t.noItems)}</p></div></section>`;
}

function videoMedia(lang, n) {
  const t = L[lang];
  return `<div class="mvp-preview-media news-video"><button class="mvp-preview-trigger" type="button" data-preview-src="${esc(n.video.url.replace(/^\/admin-media\/(.+\.mp4)$/i, "/admin-video/$1"))}" aria-label="${esc(t.watchVideo)}">`
    + `<img src="${esc(n.video.poster || n.cover)}" alt="" width="1600" height="900" loading="lazy"><span class="mvp-preview-play" aria-hidden="true">▶</span><span class="mvp-preview-duration">${esc(durationText(n.video.duration))}</span></button></div>`;
}

// 上一条 / 下一条：没有就不画（不留空白卡），有的那张留在自己那一侧
function navCard(lang, n, label, cls) {
  if (!n) return '';
  return `<a class="case-pn news-pn-card ${cls}" href="/${lang}/news/${encodeURIComponent(n.id)}/"><img src="${esc(n.cover)}" alt="" width="1600" height="900" loading="lazy"><span><small>${esc(label)}</small><b>${esc(n.title)}</b><em><time datetime="${esc(n.date)}">${esc(dateText(lang, n.date))}</time></em></span></a>`;
}

// 正文单独一行写 [图N] / 【图N】说明，N 是现场照片序号；无效编号整行不显示。
// 返回正文 HTML 和已用图号，文末照片墙只保留未插入正文的照片。
const PHOTO_MARK = /^\s*(?:\[|【)\s*图\s*([^\]】\s]*)\s*(?:\]|】)\s*(.*)$/;
function renderBody(lang, n) {
  const t = L[lang], used = new Set(), blocks = [];
  let lines = [];
  const flush = () => {
    if (lines.length) blocks.push(`<p>${esc(lines.join('\n'))}</p>`);
    lines = [];
  };
  for (const paragraph of n.paragraphs) {
    for (const line of paragraph.split(/\r?\n/)) {
      const m = line.match(PHOTO_MARK);
      if (!m) { lines.push(line); continue; }
      flush();
      const index = Number(m[1]);
      if (!Number.isInteger(index) || index < 1 || index > n.gallery.length) continue;
      used.add(index);
      const caption = m[2].trim(), url = n.gallery[index - 1];
      const label = caption || `${t.galleryTitle} ${index}`;
      blocks.push(`<figure class="news-figure"><button class="news-photo" type="button" data-full="${esc(url)}" aria-label="${esc(label)}"><img src="${esc(url)}" alt="${esc(caption)}" loading="lazy" decoding="async"></button>${caption ? `<figcaption>${esc(caption)}</figcaption>` : ''}</figure>`);
    }
    flush();
  }
  return { html: blocks.join(''), used };
}

function detailMain(lang, n, prev, next) {
  const t = L[lang];
  const figure = n.video ? videoMedia(lang, n) : `<img src="${esc(n.cover)}" alt="${esc(n.title)}" width="1600" height="900">`;
  const hero = `<section class="page-hero case-hero2 news-hero"><div class="container case-hero2-grid"><div class="case-hero2-text">`
    + crumbs(lang, [[t.label, `/${lang}/news/`], [n.title]], `/${lang}/news/`) + `<p class="news-hero-meta">${metaLine(lang, n)}</p>`
    + `<h1>${esc(n.title)}</h1><p class="intro">${esc(n.summary)}</p></div>`
    + `<figure class="case-hero2-figure news-hero-figure"${n.video ? ' id="news-video"' : ''}>${figure}</figure></div></section>`;
  // 左栏：正文（含 [图N] 插图）+ 未使用的现场照片；右栏：本篇信息 + 咨询入口。
  const rendered = renderBody(lang, n);
  const labels = ` data-label-close="${esc(t.close)}" data-label-prev="${esc(t.prevImage)}" data-label-next="${esc(t.nextImage)}"`;
  const rest = n.gallery.map((url, i) => ({ url, i })).filter(({ i }) => !rendered.used.has(i + 1));
  const main = [];
  if (rendered.html) main.push(`<div class="news-text"${rendered.used.size ? labels : ''}>${rendered.html}</div>`);
  if (rest.length) main.push(`<section class="news-gallery"><h2 class="eyebrow news-gallery-title">${esc(t.galleryTitle)}</h2><div class="news-photos"${labels}>${rest.map(({ url, i }) => `<button class="news-photo" type="button" data-full="${esc(url)}" data-index="${i}" aria-label="${esc(t.galleryTitle)} ${i + 1}"><img src="${esc(url)}" alt="" width="1600" height="1200" loading="lazy" decoding="async"></button>`).join('')}</div></section>`);
  const videoLink = n.video ? `<a class="news-info-video" href="#news-video"><span class="news-info-video-mark" aria-hidden="true">▶</span><span class="news-info-video-text"><b>${esc(t.videoReplay)}</b><small>${esc(durationText(n.video.duration))} · ${esc(t.videoNote)}</small></span><span class="news-info-video-arrow" aria-hidden="true">↑</span></a>` : '';
  const aside = `<aside class="case-aside news-aside"><div class="case-aside-card"><p class="case-aside-title">${esc(t.infoTitle)}</p>`
    + `<div class="news-info-top">${n.category ? `<span class="news-info-category">${esc(n.category)}</span>` : ''}<time datetime="${esc(n.date)}">${esc(dateText(lang, n.date))}</time></div>`
    + videoLink
    + (n.link ? `<a class="button outline news-aside-link" href="${esc(n.link)}" target="_blank" rel="noopener">${esc(linkText(lang, n.link))} ↗</a>` : '')
    + `<div class="news-aside-cta"><h2>${esc(t.ctaTitle)}</h2><p>${esc(t.ctaText)}</p><a class="button primary" href="#contact">${esc(t.consult)} <span aria-hidden="true">→</span></a></div></div></aside>`;
  const body = `<section class="svc-section case-body2 news-body"><div class="container case-body2-grid"><div class="case-main news-main">${main.join('')}</div>${aside}</div></section>`;
  const cards = navCard(lang, prev, t.prev, 'is-prev') + navCard(lang, next, t.next, 'is-next');
  const allNews = `<a class="news-all-link" href="/${lang}/news/"><span class="news-all-link-text"><small>${esc(t.allNewsKicker)}</small><b>${esc(t.allNews)}</b></span><span class="news-all-link-arrow" aria-hidden="true">→</span></a>`;
  const pn = `<section class="case-pn-section news-pn"><div class="container">${cards ? `<div class="case-pn-grid">${cards}</div>` : ''}${allNews}</div></section>`;
  return `<main id="main">${hero}${body}${pn}`;
}

// 首页“最新动态”：最新一条早于 183 天前就整块不显示。
// 3 条以上：左边头条大卡，右边清单（最多 3 条，小图 + 标题）；2 条两张一样大；1 条一张横排
function renderHome(lang, override = null) {
  const items = frontData.news(lang, override);
  if (!items.length) return '';
  const latest = new Date(items[0].date + 'T00:00:00');
  if (Number.isNaN(latest.getTime())) return '';
  if (Date.now() - latest.getTime() > 183 * 24 * 60 * 60 * 1000) return '';
  const list = items.slice(0, 4), t = L[lang], h = HOME[lang];
  const href = n => `/${lang}/news/${encodeURIComponent(n.id)}/`;
  const badge = (n, small) => n.video ? `<span class="news-video-badge home-news-video${small ? ' is-small' : ''}" aria-hidden="true">▶ ${esc(t.videoBadge)}</span>` : '';
  const card = (n, cls) => `<a class="home-news-card ${cls}" href="${href(n)}"><span class="home-news-cover"><img src="${esc(coverThumbnail(n.cover))}" alt="${esc(n.title)}" loading="lazy" decoding="async" width="1600" height="900">${badge(n)}</span><span class="home-news-body">${metaLine(lang, n)}<h3>${esc(n.title)}</h3><p class="home-news-sum">${esc(n.summary)}</p></span></a>`;
  const row = n => `<a class="home-news-row" href="${href(n)}"><span class="home-news-thumb"><img src="${esc(coverThumbnail(n.cover))}" alt="" loading="lazy" decoding="async" width="1600" height="900">${badge(n, true)}</span><span class="home-news-row-text">${metaLine(lang, n)}<h3>${esc(n.title)}</h3></span></a>`;
  let grid;
  if (list.length >= 3) {
    grid = `<div class="home-news-grid is-lead">${card(list[0], 'is-big')}<div class="home-news-list">${list.slice(1).map(row).join('')}</div></div>`;
  } else if (list.length === 2) {
    grid = `<div class="home-news-grid is-2">${card(list[0], 'is-two')}${card(list[1], 'is-two')}</div>`;
  } else {
    grid = `<div class="home-news-grid is-1">${card(list[0], 'is-one')}</div>`;
  }
  return `<section class="home-news" id="news" aria-labelledby="home-news-title"><div class="container"><div class="home-head"><div><p class="eyebrow">${esc(h.eyebrow)}</p><h2 id="home-news-title">${esc(h.title)}</h2><p>${esc(h.intro)}</p></div><a class="home-head-link" href="/${lang}/news/">${esc(h.more)} <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h16"/><path d="m14 6 6 6-6 6"/></svg></a></div>${grid}</div></section>`;
}

// 首页：把“最新动态”插到产品服务前面（公司介绍之后），再把所有“NN / ”编号按出现顺序重排。
// 没有能显示的新闻（没发布、这种语言没填完、最新一条早于 183 天）就原样返回，编号和底色都是原来的。
// 新闻块后面三块的底色对调写在 news.css（#news ~ …），新闻块不在时自动不生效。
// news.css 也在这里加（只在新闻块显示时加），server.js 的 homePage() 原来那一行不用动（方便前辈合并）。
function placeHome(html, lang, override = null) {
  const block = renderHome(lang, override);
  if (!block) return html;
  const at = html.search(/<section\b[^>]*\bid="services"/);
  if (at < 0) return html;
  html = html.slice(0, at) + block + html.slice(at);
  html = html.replace('</head>', '<link rel="stylesheet" href="/news.css"></head>');
  let n = 0;
  return html.replace(/(<p class="eyebrow[^"]*">)\d{2}( \/ )/g, (m, open, sep) => open + String(++n).padStart(2, '0') + sep);
}

// 网站地图：在 </urlset> 前补上各语言看得到的新闻（这种语言有新闻才列列表页，再列每条详情页）。
// server.js 只在 /sitemap.xml 前面加一行钩子调这里，原来生成网站地图的那一行不用动（方便前辈合并）。
function sitemap(xml) {
  const urls = [];
  for (const l of LANGS) {
    const items = frontData.news(l);
    if (!items.length) continue;
    urls.push(`${SITE}/${l}/news/`);
    for (const n of items) urls.push(`${SITE}/${l}/news/${encodeURIComponent(n.id)}/`);
  }
  if (!urls.length || !String(xml).includes('</urlset>')) return xml;
  return String(xml).replace('</urlset>', urls.map(x => `<url><loc>${x}</loc></url>`).join('\n') + '\n</urlset>');
}

function render(lang, id, publicRoot, override = null) {
  if (!L[lang]) return { status: 404, body: 'Not found' };
  const all = {};
  for (const l of LANGS) all[l] = frontData.news(l, override);
  // 这种语言一条看得到的新闻都没有：列表页按“不存在”处理，交给网站标准 404 页（body 为 null）；发布后自动恢复
  if (!all[lang].length) return { status: 404, body: null };
  if (!id) {
    const items = all[lang], t = L[lang];
    const altUrls = Object.fromEntries(LANGS.map(l => [l, `${SITE}/${l}/news/`]));
    const langHrefs = Object.fromEntries(LANGS.map(l => [l, `/${l}/news/`]));
    return {
      status: 200,
      body: shellAbout(lang, publicRoot, listMain(lang, items), {
        title: `${t.label} | EVOL DREAM`, desc: t.intro, canonical: altUrls[lang],
        image: items[0] ? SITE + items[0].cover : '', altUrls,
      }, langHrefs, { css: ['/v2-courses.css'] }),
    };
  }
  const items = all[lang], index = items.findIndex(n => n.id === id);
  if (index < 0) {
    const t = L[lang], altUrls = Object.fromEntries(LANGS.map(l => [l, `${SITE}/${l}/news/`]));
    const langHrefs = Object.fromEntries(LANGS.map(l => [l, `/${l}/news/`]));
    const main = `<main id="main"><section class="page-hero"><div class="container"><p class="eyebrow">404</p><h1>${esc(t.notFound)}</h1><div class="hero-actions"><a class="button primary" href="/${lang}/news/">← ${esc(t.label)}</a></div></div></section>`;
    return {
      status: 404,
      body: shellAbout(lang, publicRoot, main, { title: `${t.notFound} | EVOL DREAM`, desc: t.notFound, canonical: altUrls[lang], image: '', altUrls }, langHrefs),
    };
  }
  const n = items[index], prev = items[index - 1], next = items[index + 1];
  const altUrls = {}, langHrefs = {};
  for (const l of LANGS) {
    const visible = all[l].some(x => x.id === id);
    altUrls[l] = visible ? `${SITE}/${l}/news/${encodeURIComponent(id)}/` : `${SITE}/${l}/news/`;
    langHrefs[l] = visible ? `/${l}/news/${encodeURIComponent(id)}/` : `/${l}/news/`;
  }
  return {
    status: 200,
    body: shellAbout(lang, publicRoot, detailMain(lang, n, prev, next), {
      title: `${n.title} | EVOL DREAM`, desc: n.summary, canonical: altUrls[lang],
      image: n.cover ? SITE + n.cover : '', altUrls,
    }, langHrefs, { css: ['/service-page.css', '/case-page.css'], player: true }),
  };
}

module.exports = { render, renderHome, placeHome, sitemap };
