import messages from './messages.json';

export const locales = ['zh', 'vi', 'en'] as const;
export type Locale = (typeof locales)[number];
export const localeInfo = {
  zh: { label: '中文', html: 'zh-CN', og: 'zh_CN' },
  vi: { label: 'Tiếng Việt', html: 'vi', og: 'vi_VN' },
  en: { label: 'English', html: 'en', og: 'en_US' },
};
export const serviceChoices = [
  { value: 'training', label: 'AI培训' },
  { value: 'agents', label: 'Agent部署' },
  { value: 'implementation', label: '项目实施' },
  { value: 'partnerships', label: '合作交流' },
];
export function getLocale(pathname: string): Locale {
  const prefix = pathname.split('/')[1];
  return locales.includes(prefix as Locale) ? (prefix as Locale) : 'vi';
}
export function translate(locale: Locale, value: string): string {
  const key = value.replace(/\s+/g, ' ').trim();
  if (locale === 'zh' || !/[\u4e00-\u9fff]/.test(key)) return value;
  const result = (messages as Record<string, { en: string; vi: string }>)[key]?.[locale];
  if (!result) throw new Error(`Missing ${locale} translation: ${key}`);
  return result;
}
export function localPath(locale: Locale, href: string): string {
  if (!/^\/(zh|vi|en)\//.test(href)) return href;
  const url = new URL(href, 'http://local.invalid');
  url.pathname = url.pathname.replace(/^\/(zh|vi|en)\//, `/${locale}/`);
  const service = serviceChoices.find((s) => s.label === url.searchParams.get('service'));
  if (service) url.searchParams.set('service', service.value);
  return url.pathname + url.search + url.hash;
}
export function context(pathname: string) {
  const locale = getLocale(pathname);
  return { locale, t: (value: string) => translate(locale, value), p: (href: string) => localPath(locale, href) };
}
export function localizeData<T>(locale: Locale, data: T): T {
  if (typeof data === 'string') return translate(locale, localPath(locale, data)) as T;
  if (Array.isArray(data)) return data.map((item) => localizeData(locale, item)) as T;
  if (data !== null && typeof data === 'object')
    return Object.fromEntries(Object.entries(data).map(([key, value]) => [key, localizeData(locale, value)])) as T;
  return data;
}
// Only these interaction messages are sent to the browser, not the full content dictionary.
const clientKeys = [
  '打开导航菜单',
  '关闭导航菜单',
  '正在提交',
  '提交咨询',
  '暂时无法提交，请稍后重试。',
  '查看提交确认 →',
  '连接超时，尚未确认提交结果。请重试，同一次提交不会重复保存。',
  '连接失败，请保留内容并重试。',
  '本次浏览器中暂无提交记录',
  '咨询已保存至本机。记录编号：{id}。当前预览未连接商务通知。',
  '咨询已保存。记录编号：{id}。商务通知已进入邮件发送队列。',
  '此项为必填项。',
  '请阅读并同意隐私政策。',
  '请输入有效邮箱或带区号的电话号码。',
];
export const clientMessages = (locale: Locale) =>
  Object.fromEntries(clientKeys.map((key) => [key, translate(locale, key)]));
