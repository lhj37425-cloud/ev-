const ui: Record<string, string> = JSON.parse(document.querySelector('[data-ui-messages]')?.textContent || '{}');
const t = (key: string) => ui[key] || key;
const locale = document.documentElement.lang.split('-')[0];
const legacyServices: Record<string, string> = {
  AI培训: 'training',
  Agent部署: 'agents',
  项目实施: 'implementation',
  合作交流: 'partnerships',
};
const languageMenu = document.querySelector<HTMLDetailsElement>('.language-switcher');
function syncLanguageLinks() {
  document.querySelectorAll<HTMLAnchorElement>('[data-language-link]').forEach((link) => {
    const target = new URL(link.href);
    target.search = location.search;
    target.hash = location.hash;
    const service = target.searchParams.get('service');
    if (service && legacyServices[service]) target.searchParams.set('service', legacyServices[service]);
    link.href = target.href;
  });
}
syncLanguageLinks();
window.addEventListener('hashchange', syncLanguageLinks);
document.addEventListener('click', (event) => {
  if (languageMenu && !languageMenu.contains(event.target as Node)) languageMenu.open = false;
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && languageMenu?.open) {
    languageMenu.open = false;
    languageMenu.querySelector('summary')?.focus();
  }
});
const menuButton = document.querySelector<HTMLButtonElement>('.menu-toggle');
const nav = document.querySelector<HTMLElement>('.main-nav');
function closeMenu() {
  menuButton?.setAttribute('aria-expanded', 'false');
  menuButton?.setAttribute('aria-label', t('打开导航菜单'));
  nav?.classList.remove('is-open');
}
menuButton?.addEventListener('click', () => {
  const expanded = menuButton.getAttribute('aria-expanded') !== 'true';
  menuButton.setAttribute('aria-expanded', String(expanded));
  menuButton.setAttribute('aria-label', expanded ? t('关闭导航菜单') : t('打开导航菜单'));
  nav?.classList.toggle('is-open', expanded);
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && menuButton?.getAttribute('aria-expanded') === 'true') {
    closeMenu();
    menuButton?.focus();
  }
});
nav?.addEventListener('click', (event) => {
  if ((event.target as HTMLElement).closest('a')) closeMenu();
});
window.matchMedia('(min-width: 1281px)').addEventListener('change', closeMenu);

const filterButtons = document.querySelectorAll<HTMLButtonElement>('[data-filter]');
filterButtons.forEach((button) =>
  button.addEventListener('click', () => {
    filterButtons.forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
    document.querySelectorAll<HTMLElement>('[data-category]').forEach((card) => {
      card.hidden = button.dataset.filter !== 'all' && card.dataset.category !== button.dataset.filter;
    });
  })
);

const params = new URLSearchParams(location.search);
const campaignKeys = ['utm_source', 'utm_medium', 'utm_campaign'];
try {
  const campaign = Object.fromEntries(
    campaignKeys.filter((key) => params.has(key)).map((key) => [key, params.get(key)?.slice(0, 120)])
  );
  if (Object.keys(campaign).length) sessionStorage.setItem('evol-campaign', JSON.stringify(campaign));
} catch {
  /* Storage may be unavailable in private browsing. */
}

document.querySelectorAll<HTMLFormElement>('[data-lead-form]').forEach((form) => {
  const service = form.elements.namedItem('service') as HTMLSelectElement;
  const requestedService = params.get('service') || '';
  const selectedService = legacyServices[requestedService] || requestedService;
  if (['training', 'agents', 'implementation', 'partnerships'].includes(selectedService))
    service.value = selectedService;
  form.noValidate = true;
  form.addEventListener('input', (event) => {
    const field = event.target;
    if (field instanceof HTMLInputElement || field instanceof HTMLSelectElement || field instanceof HTMLTextAreaElement)
      field.setCustomValidity('');
  });
  let requestId = crypto.randomUUID();
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    form
      .querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>('input, select, textarea')
      .forEach((field) => {
        field.setCustomValidity('');
        if (field.validity.valueMissing)
          field.setCustomValidity(t(field.name === 'consent' ? '请阅读并同意隐私政策。' : '此项为必填项。'));
        if (field.name === 'contact' && field.value.trim()) {
          const value = field.value.trim();
          const validEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
          const validPhone = /^\+?[\d\s().-]{7,30}$/.test(value) && value.replace(/\D/g, '').length >= 7;
          if (!validEmail && !validPhone) field.setCustomValidity(t('请输入有效邮箱或带区号的电话号码。'));
        }
      });
    if (!form.reportValidity()) return;
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    if (button.disabled) return;
    const status = form.querySelector<HTMLElement>('.form-status')!;
    const label = form.querySelector<HTMLElement>('[data-submit-label]')!;
    const fields = Object.fromEntries(new FormData(form));
    let campaign = {};
    try {
      campaign = JSON.parse(sessionStorage.getItem('evol-campaign') || '{}');
    } catch {
      /* Optional attribution only. */
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    button.disabled = true;
    label.textContent = t('正在提交');
    status.textContent = '';
    status.classList.remove('is-error');
    try {
      const response = await fetch('/api/leads', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Site-Language': locale },
        signal: controller.signal,
        body: JSON.stringify({
          ...fields,
          consent: (form.elements.namedItem('consent') as HTMLInputElement).checked,
          language: locale,
          source: location.pathname,
          campaign,
          requestId,
        }),
      });
      const data = await response.json().catch(() => {
        throw new Error(t('暂时无法提交，请稍后重试。'));
      });
      if (!response.ok) throw new Error(data.error || t('暂时无法提交，请稍后重试。'));
      requestId = crypto.randomUUID();
      try {
        sessionStorage.setItem('evol-receipt', data.id);
      } catch {
        /* Confirmation also renders on the current page. */
      }
      form.reset();
      status.textContent = t(
        data.notification === 'queued'
          ? '咨询已保存。记录编号：{id}。商务通知已进入邮件发送队列。'
          : '咨询已保存至本机。记录编号：{id}。当前预览未连接商务通知。'
      ).replace('{id}', data.id);
      status.focus();
      const receipt = document.createElement('a');
      receipt.href = `/${locale}/thank-you/`;
      receipt.textContent = t('查看提交确认 →');
      receipt.className = 'receipt-link';
      status.append(document.createElement('br'), receipt);
    } catch (error) {
      status.classList.add('is-error');
      status.textContent =
        error instanceof Error && error.name === 'AbortError'
          ? t('连接超时，尚未确认提交结果。请重试，同一次提交不会重复保存。')
          : error instanceof Error && !(error instanceof TypeError)
            ? error.message
            : t('连接失败，请保留内容并重试。');
      status.focus();
    } finally {
      clearTimeout(timeout);
      button.disabled = false;
      label.textContent = t('提交咨询');
    }
  });
});
const receiptElement = document.querySelector('[data-receipt]');
if (receiptElement) {
  let id = '';
  try {
    id = sessionStorage.getItem('evol-receipt') || '';
  } catch {
    /* No stored receipt. */
  }
  receiptElement.textContent = id || t('本次浏览器中暂无提交记录');
}
