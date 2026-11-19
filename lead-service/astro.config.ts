import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  site: process.env.SITE_URL || 'http://127.0.0.1:4321',
  output: 'static',
  trailingSlash: 'always',
  integrations: [
    sitemap({
      filter: (page) => new URL(page).pathname !== '/' && !page.includes('/thank-you/') && !page.includes('/404'),
      i18n: { defaultLocale: 'vi', locales: { zh: 'zh-CN', vi: 'vi', en: 'en' } },
    }),
  ],
  vite: { plugins: [tailwindcss()] },
});
