# pasameloenespañol.com

¿Cómo no va a saber inglés en big 2026? 💀 Pegás el link de un reel de Instagram o un TikTok en inglés y te devuelve un link con el video subtitulado al español, listo para mandárselo a quien no sabe inglés. Gratis, sin cuenta, y el link dura 24 horas.

Hecho por [Mat · Fool Stack Dev](https://www.instagram.com/le.mat1337/) como proyecto abierto: el código está acá para que lo leas, lo critiques y lo uses.

## Cómo funciona

```
link ──► validar (lista blanca) ──► Turnstile ──► cupos (3 por día por IP)
     ──► yt-dlp (H.264 ≤ 720p) ──► ffmpeg (audio 16 kHz) ──► Groq Whisper (inglés)
     ──► traducción a español rioplatense (JSON validado) ──► subtítulos cortos
     ──► video en Cloudflare R2 + subtítulos en Redis (24 h) ──► /v/<id>
```

- **Frontend:** Astro estático (`web/`). Los subtítulos se dibujan encima del video, no con `<track>`: en iOS no se pueden estilar. Hay un botón para pasarlos arriba cuando el reel ya trae texto abajo, y otro de pantalla grande que agranda el marco entero para que los subtítulos sigan.
- **Backend:** FastAPI en Python 3.12 (`backend/`). Un pedido procesa el video en la misma request y va mandando el progreso en NDJSON; si el celu corta, el cliente sigue por `GET /api/jobs/<id>`.
- **Todo en un proyecto de Vercel** con [Services](https://vercel.com/docs/services): `web` y `backend` en un solo deploy. Estado y cupos en Upstash Redis, videos en Cloudflare R2 (bucket privado, URLs firmadas que vencen con el job), captcha con Cloudflare Turnstile, transcripción y traducción con Groq (o Claude Haiku, elegible por variable de entorno). Un cron diario borra los videos vencidos.
- **Traducción:** Whisper corta sus segmentos donde quiere, así que primero se arman frases completas (por fin de oración o pausa) y se traducen de a una, con esquema JSON estricto; si el modelo junta o corre frases, se reintenta. Después la traducción se reparte en subtítulos de hasta 64 caracteres.

## Cuánto cuesta

USD 0. Corre en el plan Hobby (gratis) de Vercel y en los tiers gratis de Groq, Upstash, Turnstile y R2. Hobby no cobra nunca: si un recurso pasa su tope, Vercel lo corta. Por eso hay topes globales por hora y por día (`PES_MAX_JOBS_HORA`, `PES_MAX_JOBS_DIA`) que frenan antes, y los videos van a R2, que no cobra la transferencia.

## Seguridad

El repo es público y cualquiera puede buscarle la vuelta; el objetivo es que abusar del sitio no cueste plata ni exponga a nadie. En capas:

1. **WAF de Vercel:** mitigación de DDoS y una regla de rate limit por IP en `/api/`.
2. **Turnstile** obligatorio, verificado del lado del servidor con `action` y hostname; sin clave en producción, no se procesa nada.
3. **Cupos:** un job en curso por IP, 3 por día por IP (IPv6 agrupado por /64, IP hasheada con una sal que rota por día, nunca guardada) y topes globales por hora y por día. Los errores que son culpa del sitio devuelven el cupo; los del usuario, no.
4. **Caché:** el mismo video se procesa una vez; las repeticiones no gastan nada.
5. **Entrada:** lista blanca exacta de hosts, solo `http(s)`, sin credenciales ni puertos, una regex por plataforma, links cortos resueltos a mano (3 saltos, cada uno vuelve a la lista blanca), cuerpo de 8 KB como máximo.
6. **Subprocesos:** yt-dlp y ffmpeg con lista de argumentos (sin shell), `timeout`, carpeta temporal propia, entorno mínimo sin secretos, sin config ni caché de yt-dlp, archivo de salida verificado dentro de la carpeta. Nunca se usan cookies ni cuentas de Instagram o TikTok.
7. **Contenido de terceros:** el texto del video es dato, no instrucciones: esquema JSON estricto, índices y largos validados, tags y caracteres de control filtrados, y el frontend lo pinta con `textContent`.
8. **Frontend:** CSP estricta sin `unsafe-inline` (nada inline en el build), HSTS, `nosniff`, `X-Frame-Options: DENY`, COOP, Permissions-Policy, `/v/*` con `noindex`, IDs de 96 bits, sin listado de jobs.
9. **Errores y datos:** mensajes fijos; nunca se devuelve stderr, stack traces ni nombres de variables; sin docs públicas de la API; los logs no tienen IP ni URLs firmadas.
10. **Cron** solo con `Authorization: Bearer $CRON_SECRET` (comparación en tiempo constante).
11. **Secretos y dependencias:** todo por variables de entorno (`.env.example` documenta todas), gitleaks en CI y en pre-commit, Dependabot semanal, lockfiles commiteados.

Para reportar algo: [`SECURITY.md`](SECURITY.md).

## Correrlo en tu compu

Necesitás Node 22.23.4+, [uv](https://docs.astral.sh/uv/) y, para transcribir de verdad, una clave gratis de [Groq](https://console.groq.com).

```bash
# backend
cd backend
uv sync
PES_DEMO=1 uv run uvicorn app:app --port 8000   # demo: baja el video de verdad, subtítulos de prueba, sin claves

# frontend (en otra terminal)
cd web
npm install
npm run dev                                     # http://localhost:4321, /api va al 8000
```

Sin `PES_DEMO`, el backend usa Groq: copiá `.env.example` a `backend/.env` (o exportá las variables) con `GROQ_API_KEY`.

Pipeline por consola, sin servidor:

```bash
cd backend
uv run python -m pes.cli "https://www.tiktok.com/@alguien/video/123..."   # imprime el WebVTT en español
uv run python -m pes.cli "<link>" --solo-bajar                            # solo descarga y audio
```

Tests:

```bash
cd backend && uv run pytest && uv run ruff check .
cd web && npx astro check && npm run build
```

## Deploy en Vercel

1. Importá el repo en Vercel. Alcanza el plan Hobby: `vercel.json` ya define los dos services, las rewrites, el cron diario y las cabeceras de seguridad.
2. Conectá **Upstash Redis** desde el Marketplace: las variables se cargan solas.
3. Videos: un bucket privado de **Cloudflare R2** con un token "Object Read & Write" solo para ese bucket (`R2_*`) y una regla de ciclo de vida que borre a los 4 días como respaldo. Sin R2, conectá un store público de **Vercel Blob** (en Hobby, su transferencia de 10 GB por mes es el punto débil).
4. Creá un widget de **Turnstile** en Cloudflare (gratis) con el dominio.
5. Cargá las variables de [`.env.example`](.env.example): `GROQ_API_KEY`, `TURNSTILE_SECRET_KEY`, `PUBLIC_TURNSTILE_SITE_KEY`, `CRON_SECRET`, `PES_SAL`, `PES_HOSTS` y las `R2_*`.
6. En el dashboard, una regla de rate limit del WAF para `/api/`.

## Licencia

MIT. Este proyecto no está afiliado a Meta, Instagram, ByteDance ni TikTok.

---

### English

**pasameloenespañol.com** ("pass it to me in Spanish") takes an English Instagram Reel or TikTok link and returns a 24-hour link with Spanish subtitles. Free, no account. Astro + FastAPI on Vercel Services, Groq Whisper for transcription, an LLM for translation, Upstash Redis and Cloudflare R2 for storage, Cloudflare Turnstile and per-IP/global quotas to keep the cost at zero. MIT licensed.
