export const GET = () =>
  new Response(
    import.meta.env.SITE_URL
      ? `User-agent: *\nAllow: /\nSitemap: ${import.meta.env.SITE_URL}/sitemap-index.xml\n`
      : 'User-agent: *\nDisallow: /\n',
    { headers: { 'Content-Type': 'text/plain' } }
  );
