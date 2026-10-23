// Formulario de la home: Turnstile -> POST /api/jobs -> progreso (NDJSON) -> /v/<id>.
import { ErrorApi, SIN_RED, esperarJob, guardarSesion, leerSesion, registrar, type Evento } from './api';

type Turnstile = {
  render: (el: HTMLElement, opciones: Record<string, unknown>) => string;
  execute: (id: string) => void;
  reset: (id: string) => void;
};
declare global {
  interface Window {
    turnstile?: Turnstile;
  }
}

const CLAVE_SESION = 'pes:job';
const ETAPAS = ['descargando', 'escuchando', 'traduciendo', 'guardando'];

const form = document.getElementById('form') as HTMLFormElement;
const input = document.getElementById('link') as HTMLInputElement;
const enviar = document.getElementById('enviar') as HTMLButtonElement;
const pegar = document.getElementById('pegar') as HTMLButtonElement;
const caja = document.getElementById('captcha') as HTMLDivElement;
const cajaError = document.getElementById('error') as HTMLParagraphElement;
const espera = document.getElementById('espera') as HTMLDivElement;
const aviso = document.getElementById('aviso') as HTMLParagraphElement;
const pasos = Array.from(document.querySelectorAll<HTMLLIElement>('#progreso li'));
const siteKey = form.dataset.sitekey ?? '';

let widget: string | null = null;
let esperandoToken: ((token: string) => void) | null = null;
let fallaToken: ((e: Error) => void) | null = null;
let relojToken: number | undefined;
let ocupado = false;

function mostrarError(mensaje: string | null): void {
  cajaError.hidden = !mensaje;
  cajaError.textContent = mensaje ?? '';
}

function avanzar(etapa: string): void {
  const i = ETAPAS.indexOf(etapa);
  if (i < 0) return;
  pasos.forEach((li, j) => {
    li.classList.toggle('hecho', j < i);
    li.classList.toggle('activo', j === i);
  });
}

function cargando(si: boolean): void {
  ocupado = si;
  enviar.disabled = si;
  input.readOnly = si;
  enviar.firstChild!.textContent = si ? 'Verificando… ' : 'Pasámelo ';
  if (!si) {
    espera.hidden = true;
    aviso.hidden = true;
  }
}

// Los pasos aparecen recién con el pedido en marcha (no mientras Turnstile verifica).
function mostrarProgreso(): void {
  enviar.firstChild!.textContent = 'Traduciendo… ';
  if (!espera.hidden) return;
  espera.hidden = false;
  pasos.forEach((li) => li.classList.remove('hecho', 'activo'));
  avanzar('descargando');
}

function irAlVideo(id: string): void {
  guardarSesion(CLAVE_SESION, null);
  pasos.forEach((li) => {
    li.classList.remove('activo');
    li.classList.add('hecho');
  });
  location.assign(`/v/${id}`);
}

// --- Turnstile (se ejecuta recién al tocar el botón: el token nunca llega vencido) ---
function prepararTurnstile(): void {
  if (!siteKey || widget || !window.turnstile) return;
  widget = window.turnstile.render(caja, {
    sitekey: siteKey,
    action: 'pasamelo',
    execution: 'execute',
    appearance: 'interaction-only',
    language: 'es',
    callback: (token: string) => esperandoToken?.(token),
    'error-callback': () => fallaToken?.(new ErrorApi('captcha', 'No pude confirmar que sos una persona. Probá de nuevo.')),
    // Si Cloudflare pide tocar la casilla, una persona necesita tiempo: se apaga el reloj y se avisa.
    'before-interactive-callback': () => {
      window.clearTimeout(relojToken);
      aviso.hidden = false;
    },
    'after-interactive-callback': () => {
      aviso.hidden = true;
    },
    'timeout-callback': () => fallaToken?.(new ErrorApi('captcha', 'La verificación se venció. Tocá Pasámelo de nuevo.')),
  });
}

function pedirToken(): Promise<string | null> {
  if (!siteKey) return Promise.resolve(null); // desarrollo sin captcha
  prepararTurnstile();
  if (!window.turnstile || !widget) {
    return Promise.reject(new ErrorApi('captcha', 'No cargó la verificación. Recargá la página y probá de nuevo.'));
  }
  const ts = window.turnstile;
  const id = widget;
  return new Promise((ok, mal) => {
    window.clearTimeout(relojToken);
    relojToken = window.setTimeout(
      () => mal(new ErrorApi('captcha', 'La verificación tardó demasiado. Probá de nuevo.')),
      25_000,
    );
    esperandoToken = (token) => {
      window.clearTimeout(relojToken);
      aviso.hidden = true;
      ok(token);
    };
    fallaToken = (e) => {
      window.clearTimeout(relojToken);
      aviso.hidden = true;
      mal(e);
    };
    ts.reset(id);
    ts.execute(id);
  });
}

// --- Pedido y progreso ---
async function leerStream(resp: Response): Promise<Evento | null> {
  const lector = resp.body?.getReader();
  if (!lector) return null;
  const decodificador = new TextDecoder();
  let buffer = '';
  let ultimo: Evento | null = null;
  try {
    for (;;) {
      const { value, done } = await lector.read();
      if (done) break;
      buffer += decodificador.decode(value, { stream: true });
      let corte: number;
      while ((corte = buffer.indexOf('\n')) >= 0) {
        const linea = buffer.slice(0, corte).trim();
        buffer = buffer.slice(corte + 1);
        if (!linea) continue;
        const evento = JSON.parse(linea) as Evento;
        if (evento.id) guardarSesion(CLAVE_SESION, evento.id);
        if (evento.etapa === 'latido') continue;
        ultimo = evento;
        avanzar(evento.etapa);
        if (evento.etapa === 'listo' || evento.etapa === 'error') return evento;
      }
    }
  } catch {
    /* se cortó: seguimos por consulta */
  }
  return ultimo;
}

async function pasamelo(link: string): Promise<void> {
  const token = await pedirToken();
  mostrarProgreso();
  let resp: Response;
  try {
    resp = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/x-ndjson, application/json' },
      body: JSON.stringify({ url: link, turnstile: token }),
    });
  } catch {
    throw new ErrorApi('red', SIN_RED);
  }

  const tipo = resp.headers.get('content-type') ?? '';
  if (!tipo.includes('ndjson')) {
    const datos = (await resp.json().catch(() => ({}))) as Evento;
    if (!resp.ok) throw new ErrorApi(datos.codigo ?? 'interno', datos.mensaje ?? SIN_RED, resp.status);
    if (datos.id && datos.etapa === 'listo') return irAlVideo(datos.id);
    if (datos.id) return seguir(datos.id); // otra persona ya lo estaba traduciendo
    throw new ErrorApi('interno', SIN_RED);
  }

  const final = await leerStream(resp);
  if (final?.etapa === 'listo' && final.id) return irAlVideo(final.id);
  if (final?.etapa === 'error') throw new ErrorApi(final.codigo ?? 'interno', final.mensaje ?? SIN_RED);
  const id = final?.id ?? leerSesion(CLAVE_SESION);
  if (!id) throw new ErrorApi('interno', SIN_RED);
  return seguir(id);
}

async function seguir(id: string): Promise<void> {
  mostrarProgreso();
  guardarSesion(CLAVE_SESION, id);
  const job = await esperarJob(id, avanzar);
  if (job.estado === 'listo') return irAlVideo(job.id);
  guardarSesion(CLAVE_SESION, null);
  throw new ErrorApi(job.codigo ?? 'interno', job.mensaje ?? SIN_RED);
}

async function correr(tarea: () => Promise<void>): Promise<void> {
  if (ocupado) return;
  mostrarError(null);
  cargando(true);
  try {
    await tarea();
    registrar('job_ok');
  } catch (e) {
    const err = e instanceof ErrorApi ? e : new ErrorApi('interno', SIN_RED);
    mostrarError(err.message);
    registrar('job_error', { motivo: err.codigo });
    cargando(false);
  }
}

form.addEventListener('submit', (ev) => {
  ev.preventDefault();
  const link = input.value.trim();
  if (!link) {
    mostrarError('Primero pegá el link del reel o del TikTok, capo.');
    input.focus();
    return;
  }
  void correr(() => pasamelo(link));
});

// Botón "Pegar" donde el navegador lo permite (en iOS aparece la burbuja de "Pegar").
if (navigator.clipboard?.readText) {
  pegar.hidden = false;
  pegar.addEventListener('click', async () => {
    try {
      const texto = (await navigator.clipboard.readText()).trim();
      if (texto) input.value = texto.slice(0, 2048);
      input.focus();
    } catch {
      input.focus();
    }
  });
}

// Link precargado: /?link=... (o ?url= / ?text= de "Compartir con" en Android).
const parametros = new URLSearchParams(location.search);
const precargado = parametros.get('link') ?? parametros.get('url') ?? parametros.get('text');
if (precargado) input.value = precargado.slice(0, 2048);

// Si se recargó la página mientras traducía, retoma.
const pendiente = leerSesion(CLAVE_SESION);
if (pendiente && /^[A-Za-z0-9_-]{16}$/.test(pendiente)) void correr(() => seguir(pendiente));

// Turnstile carga async: se prepara apenas esté.
const intervalo = window.setInterval(() => {
  if (window.turnstile) {
    window.clearInterval(intervalo);
    prepararTurnstile();
  }
}, 300);
window.setTimeout(() => window.clearInterval(intervalo), 20_000);
