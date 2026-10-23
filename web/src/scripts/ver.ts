// Reproductor de /v/<id>: video 9:16 con los subtítulos dibujados encima (no ::cue: en iOS no se pueden estilar).
// Dos pistas: lo que se dice (voz o letra) y, en canciones y videos sin voz, el texto escrito traducido
// en el lugar del original.
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
const escrito = $<HTMLDivElement>('escrito');
const pistas = $<HTMLDivElement>('pistas');
const botonVoz = $<HTMLButtonElement>('pista-voz');
const botonTexto = $<HTMLButtonElement>('pista-texto');
const caption = $<HTMLDivElement>('caption');
const captionDe = $<HTMLParagraphElement>('caption-de');
const captionTexto = $<HTMLParagraphElement>('caption-texto');
const lista = $<HTMLDetailsElement>('lista-texto');
const listaItems = $<HTMLOListElement>('lista-texto-items');

type Zona = 'top' | 'mid' | 'bot';
const ZONAS: Zona[] = ['top', 'mid', 'bot'];
const zonas: Record<Zona, HTMLSpanElement> = {
  top: $<HTMLSpanElement>('escrito-top'),
  mid: $<HTMLSpanElement>('escrito-mid'),
  bot: $<HTMLSpanElement>('escrito-bot'),
};

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
let textos: Cue[] = [];
let actual = -1;
let animacion = 0;
const enZona: Record<Zona, string> = { top: '', mid: '', bot: '' };

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
  if (textos.length) pintarTexto(video.currentTime);
  if (video.duration) {
    const pct = (video.currentTime / video.duration) * 100;
    barra.style.width = `${pct}%`;
    avance.setAttribute('aria-valuenow', String(Math.round(pct)));
  }
}

// El texto escrito puede tener varios a la vez (un título arriba y una frase abajo): uno por zona.
function pintarTexto(t: number): void {
  for (const z of ZONAS) {
    const ahora = textos
      .filter((c) => (c.p ?? 'mid') === z && t >= c.s && t < c.e)
      .map((c) => c.t)
      .join('\n');
    if (ahora !== enZona[z]) {
      enZona[z] = ahora;
      zonas[z].hidden = !ahora;
      zonas[z].textContent = ahora; // textContent: nunca HTML
    }
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
// Con texto escrito traducido, la voz va donde menos lo tapa; si no, donde la dejó la persona.
function arrancarArriba(): boolean {
  if (textos.length) {
    const tapado = (z: Zona) => textos.filter((c) => c.p === z).reduce((suma, c) => suma + c.e - c.s, 0);
    return tapado('bot') > tapado('top');
  }
  try {
    return localStorage.getItem(CLAVE_POSICION) === '1';
  } catch {
    return false;
  }
}
mover.addEventListener('click', (ev) => {
  ev.stopPropagation();
  ponerArriba(!subs.classList.contains('arriba'));
  registrar('mover_subtitulos');
});

// Prender y apagar cada pista (solo se muestran los botones si hay las dos).
function alternar(boton: HTMLButtonElement, capa: HTMLElement, pista: string): void {
  boton.addEventListener('click', () => {
    const si = boton.getAttribute('aria-pressed') !== 'true';
    boton.setAttribute('aria-pressed', String(si));
    capa.classList.toggle('apagada', !si);
    registrar('pista', { [pista]: si ? 'on' : 'off' });
  });
}
alternar(botonVoz, subs, 'voz');
alternar(botonTexto, escrito, 'texto');

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

function minutos(s: number): string {
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}

function mostrarTexto(job: Job): void {
  mover.hidden = !cues.length; // sin voz no hay subtítulos que mover
  pistas.hidden = !(cues.length && textos.length);
  botonVoz.textContent = job.cancion ? 'Letra' : 'Voz';
  subs.classList.toggle('arriba', arrancarArriba());
  if (textos.length) {
    listaItems.replaceChildren(
      ...textos.map((c) => {
        const li = document.createElement('li');
        const ir = document.createElement('button');
        ir.type = 'button';
        ir.className = 'tiempo';
        ir.textContent = minutos(c.s);
        ir.setAttribute('aria-label', `Ir al ${minutos(c.s)}`);
        ir.addEventListener('click', () => {
          video.currentTime = c.s;
          pintar();
        });
        const texto = document.createElement('span');
        texto.textContent = c.t;
        li.append(ir, texto);
        return li;
      }),
    );
    lista.hidden = false;
  }
  if (job.caption) {
    captionDe.textContent = job.autor ? `Lo que escribió ${job.autor} en el posteo:` : 'Lo que dice el posteo:';
    captionTexto.textContent = job.caption;
    caption.hidden = false;
  }
}

function mostrarVideo(job: Job): void {
  cues = (job.cues ?? []).slice().sort((a, b) => a.s - b.s);
  textos = (job.texto ?? []).slice().sort((a, b) => a.s - b.s);
  mostrarTexto(job);
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
