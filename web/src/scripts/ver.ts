// Reproductor de /v/<id>: video 9:16 con los subtítulos dibujados encima (no ::cue: en iOS no se pueden estilar).
import { ErrorApi, NO_ENCONTRADO, RE_ID, esperarJob, leerJob, registrar, type Cue, type Job } from './api';

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const reproductor = $<HTMLDivElement>('reproductor');
const marco = $<HTMLDivElement>('marco');
const video = $<HTMLVideoElement>('video');
const sub = $<HTMLSpanElement>('sub');
const subs = $<HTMLDivElement>('subs');
const mover = $<HTMLButtonElement>('mover');
const grande = $<HTMLButtonElement>('grande');
const grandeTexto = $<HTMLSpanElement>('grande-texto');
const play = $<HTMLButtonElement>('play');
const avance = $<HTMLDivElement>('avance');
const barra = $<HTMLDivElement>('barra');
const expira = $<HTMLSpanElement>('expira');
const credito = $<HTMLSpanElement>('credito');
const whatsapp = $<HTMLAnchorElement>('whatsapp');
const copiar = $<HTMLButtonElement>('copiar');
const compartir = $<HTMLButtonElement>('compartir');
const estado = $<HTMLDivElement>('estado');
const estadoTitulo = $<HTMLHeadingElement>('estado-titulo');
const estadoTexto = $<HTMLParagraphElement>('estado-texto');
const estadoCta = $<HTMLAnchorElement>('estado-cta');

const id = location.pathname.split('/').filter(Boolean)[1] ?? new URLSearchParams(location.search).get('id') ?? '';
const linkPropio = `${location.origin}/v/${id}`;

function mostrarEstado(titulo: string, texto = '', cta = false): void {
  reproductor.hidden = true;
  estado.hidden = false;
  estadoTitulo.textContent = titulo;
  estadoTexto.textContent = texto;
  estadoCta.hidden = !cta;
}

// --- subtítulos ---
let cues: Cue[] = [];
let actual = -1;
let animacion = 0;

function buscarCue(t: number): number {
  let lo = 0;
  let hi = cues.length - 1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (t < cues[m].s) hi = m - 1;
    else if (t >= cues[m].e) lo = m + 1;
    else return m;
  }
  return -1;
}

function pintar(): void {
  const i = buscarCue(video.currentTime);
  if (i !== actual) {
    actual = i;
    sub.hidden = i < 0;
    sub.textContent = i < 0 ? '' : cues[i].t; // textContent: nunca HTML
  }
  if (video.duration) {
    const pct = (video.currentTime / video.duration) * 100;
    barra.style.width = `${pct}%`;
    avance.setAttribute('aria-valuenow', String(Math.round(pct)));
  }
}

function bucle(): void {
  pintar();
  animacion = requestAnimationFrame(bucle);
}

video.addEventListener('play', () => {
  play.hidden = true;
  cancelAnimationFrame(animacion);
  bucle();
});
video.addEventListener('pause', () => {
  play.hidden = false;
  cancelAnimationFrame(animacion);
  pintar();
});
video.addEventListener('ended', () => {
  play.hidden = false;
  registrar('video_completo');
});
video.addEventListener('seeked', pintar);
video.addEventListener('error', () => mostrarEstado('Ese video ya se borró', NO_ENCONTRADO, true));

// Muchos reels ya traen texto quemado abajo: los subtítulos se pueden pasar arriba (se recuerda).
const CLAVE_POSICION = 'pes:subs-arriba';
function ponerArriba(si: boolean): void {
  subs.classList.toggle('arriba', si);
  try {
    localStorage.setItem(CLAVE_POSICION, si ? '1' : '0');
  } catch {
    /* sin almacenamiento */
  }
}
try {
  if (localStorage.getItem(CLAVE_POSICION) === '1') subs.classList.add('arriba');
} catch {
  /* sin almacenamiento */
}
mover.addEventListener('click', (ev) => {
  ev.stopPropagation();
  ponerArriba(!subs.classList.contains('arriba'));
  registrar('mover_subtitulos');
});

// Pantalla grande: el marco entero, para que los subtítulos sigan. Si el navegador no deja
// (iPhone viejo), se agranda con CSS a toda la pantalla.
type ConFullscreen = HTMLElement & { webkitRequestFullscreen?: () => Promise<void> | void };
type DocConFullscreen = Document & { webkitFullscreenElement?: Element | null; webkitExitFullscreen?: () => Promise<void> | void };
const doc = document as DocConFullscreen;

function enGrande(): boolean {
  return !!(doc.fullscreenElement ?? doc.webkitFullscreenElement) || marco.classList.contains('grande-css');
}
function pintarGrande(): void {
  const si = enGrande();
  grandeTexto.textContent = si ? 'Salir' : 'Pantalla grande';
  grande.setAttribute('aria-label', si ? 'Salir de pantalla grande' : 'Ver en pantalla grande');
}
async function salirDeGrande(): Promise<void> {
  marco.classList.remove('grande-css');
  if (doc.fullscreenElement ?? doc.webkitFullscreenElement) {
    await (doc.exitFullscreen?.() ?? doc.webkitExitFullscreen?.() ?? Promise.resolve()).catch(() => {});
  }
  pintarGrande();
}
grande.addEventListener('click', async (ev) => {
  ev.stopPropagation();
  if (enGrande()) return salirDeGrande();
  const el = marco as ConFullscreen;
  const pedir = el.requestFullscreen?.bind(el) ?? el.webkitRequestFullscreen?.bind(el);
  try {
    if (!pedir) throw new Error('sin fullscreen');
    await pedir();
  } catch {
    marco.classList.add('grande-css');
  }
  pintarGrande();
  registrar('pantalla_grande');
});
document.addEventListener('fullscreenchange', pintarGrande);
document.addEventListener('webkitfullscreenchange', pintarGrande);
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && marco.classList.contains('grande-css')) void salirDeGrande();
});

marco.addEventListener('click', (ev) => {
  if (ev.target instanceof Node && avance.contains(ev.target)) return;
  if (video.paused) void video.play().catch(() => (play.hidden = false));
  else video.pause();
});

function irA(clientX: number): void {
  const r = avance.getBoundingClientRect();
  if (video.duration) video.currentTime = Math.min(Math.max((clientX - r.left) / r.width, 0), 1) * video.duration;
}
avance.addEventListener('click', (ev) => irA(ev.clientX));
avance.addEventListener('keydown', (ev) => {
  if (ev.key === 'ArrowRight') video.currentTime += 5;
  if (ev.key === 'ArrowLeft') video.currentTime -= 5;
});

// --- compartir ---
const textoCompartir = 'Te lo paso en español, capo 💀';
whatsapp.href = `https://wa.me/?text=${encodeURIComponent(`${textoCompartir}: ${linkPropio}`)}`;
whatsapp.addEventListener('click', () => registrar('compartir', { via: 'whatsapp' }));

copiar.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(linkPropio);
    copiar.textContent = '¡Copiado!';
  } catch {
    copiar.textContent = 'No se pudo copiar';
  }
  registrar('compartir', { via: 'copiar' });
  window.setTimeout(() => (copiar.textContent = 'Copiar link'), 1800);
});

if (navigator.share) {
  compartir.addEventListener('click', () => {
    registrar('compartir', { via: 'nativo' });
    navigator.share({ title: 'pasámelo en español', text: textoCompartir, url: linkPropio }).catch(() => {});
  });
} else {
  compartir.hidden = true;
  copiar.parentElement?.classList.remove('fila');
}

function tiempoRestante(expiraSeg: number): string {
  const min = Math.max(0, Math.round((expiraSeg * 1000 - Date.now()) / 60_000));
  if (min < 60) return `Se borra en ${min} min`;
  return `Se borra en ${Math.round(min / 60)} h`;
}

function mostrarVideo(job: Job): void {
  cues = (job.cues ?? []).slice().sort((a, b) => a.s - b.s);
  video.src = job.video_url ?? '';
  const plataforma = job.plataforma === 'tiktok' ? 'TikTok' : 'Instagram';
  credito.textContent = '';
  const enlace = document.createElement('a');
  enlace.href = job.original ?? '#';
  enlace.rel = 'noopener noreferrer nofollow';
  enlace.target = '_blank';
  enlace.textContent = job.autor ? `Video de ${job.autor} en ${plataforma}` : `Ver el original en ${plataforma}`;
  credito.append(enlace);
  if (job.expira) {
    expira.textContent = tiempoRestante(job.expira);
    window.setInterval(() => (expira.textContent = tiempoRestante(job.expira!)), 60_000);
  }
  estado.hidden = true;
  reproductor.hidden = false;
  pintar();
}

async function iniciar(): Promise<void> {
  if (!RE_ID.test(id)) return mostrarEstado('Ese link no existe', 'Revisá que lo hayas copiado entero.', true);
  try {
    let job = await leerJob(id);
    if (!job) return mostrarEstado('Ese video ya se borró', NO_ENCONTRADO, true);
    if (job.estado === 'procesando') {
      mostrarEstado('Lo estoy traduciendo…', 'Tarda menos de un minuto. No cierres esta pestaña.');
      job = await esperarJob(id, () => {});
    }
    if (job.estado === 'error') return mostrarEstado('No lo pude traducir', job.mensaje ?? '', true);
    mostrarVideo(job);
    registrar('ver');
  } catch (e) {
    const mensaje = e instanceof ErrorApi ? e.message : 'Algo falló. Recargá la página y probá de nuevo.';
    mostrarEstado('Uy, se rompió algo', mensaje, true);
  }
}

void iniciar();
