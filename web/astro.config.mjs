import { defineConfig } from 'astro/config';

// En desarrollo, /v/<id> sirve la página del reproductor (en Vercel lo hace la rewrite de vercel.json)
// y /api va al backend local (uvicorn en el puerto 8000).
const reproductorEnDev = {
  name: 'pes-reproductor-dev',
  /** @param {import('vite').ViteDevServer} server */
  configureServer(server) {
    server.middlewares.use((req, _res, next) => {
      if (req.url && /^\/v\/[A-Za-z0-9_-]{16}\/?(\?.*)?$/.test(req.url)) req.url = '/v/';
      next();
    });
  },
};

export default defineConfig({
  // Punycode de pasameloenespañol.com (Vercel y los navegadores lo manejan así)
  site: process.env.PUBLIC_SITE_URL || 'https://xn--pasameloenespaol-kub.com',
  compressHTML: true,
  build: { inlineStylesheets: 'never' }, // nada inline: la CSP no necesita 'unsafe-inline' para scripts
  vite: {
    build: { assetsInlineLimit: 0 },
    plugins: [reproductorEnDev],
    server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  },
});
