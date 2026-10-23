// Tipos y ayudas compartidas entre el formulario y el reproductor.

// p: solo en el texto escrito sobre el video, dónde estaba el original.
export type Cue = { s: number; e: number; t: string; p?: 'top' | 'mid' | 'bot' };

export type Job = {
  id: string;
  estado: 'procesando' | 'listo' | 'error';
  etapa?: string;
  plataforma?: 'instagram' | 'tiktok';
  original?: string;
  autor?: string | null;
  duracion?: number;
  video_url?: string;
  cues?: Cue[];
  texto?: Cue[];
  caption?: string;
  cancion?: boolean;
  expira?: number;
  codigo?: string;
  mensaje?: string;
};

export type Evento = { etapa: string; id?: string; url?: string; codigo?: string; mensaje?: string };

export const RE_ID = /^[A-Za-z0-9_-]{16}$/;
export const SIN_RED = 'No hay internet, capo. Revisá la conexión y probá de nuevo.';
export const NO_ENCONTRADO = 'Ese video ya se borró: duran 24 h. Pedí que te lo pasen de nuevo.';

export class ErrorApi extends Error {
  constructor(
    public codigo: string,
    mensaje: string,
    public status = 0,
  ) {
    super(mensaje);
  }
}

/** GET /api/jobs/<id>. null si no existe (vencido). */
export async function leerJob(id: string): Promise<Job | null> {
  let resp: Response;
  try {
    resp = await fetch(`/api/jobs/${encodeURIComponent(id)}`, { headers: { accept: 'application/json' } });
  } catch {
    throw new ErrorApi('red', SIN_RED);
  }
  if (resp.status === 404) return null;
  const datos = (await resp.json().catch(() => null)) as Job | { codigo: string; mensaje: string } | null;
  if (!resp.ok || !datos || !('id' in datos)) {
    const d = datos as { codigo?: string; mensaje?: string } | null;
    throw new ErrorApi(d?.codigo ?? 'interno', d?.mensaje ?? SIN_RED, resp.status);
  }
  return datos;
}

/** Espera a que un job termine consultando cada 2,5 s (si se cortó el stream o se recargó la página). */
export async function esperarJob(id: string, alAvanzar: (etapa: string) => void, maxMs = 7 * 60_000): Promise<Job> {
  const fin = Date.now() + maxMs;
  while (Date.now() < fin) {
    const job = await leerJob(id).catch((e: unknown) => {
      if (e instanceof ErrorApi && e.codigo === 'red') return undefined; // reintenta
      throw e;
    });
    if (job === null) throw new ErrorApi('no_encontrado', NO_ENCONTRADO, 404);
    if (job) {
      if (job.estado !== 'procesando') return job;
      if (job.etapa) alAvanzar(job.etapa);
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
  throw new ErrorApi('cortado', 'Está tardando demasiado. Probá de nuevo en un rato.');
}

/** Evento de Vercel Web Analytics, si el script cargó. Nunca rompe nada. */
export function registrar(nombre: string, datos?: Record<string, string | number>): void {
  try {
    (window as unknown as { va?: (t: string, d: unknown) => void }).va?.('event', { name: nombre, data: datos });
  } catch {
    /* sin analítica */
  }
}

export function guardarSesion(clave: string, valor: string | null): void {
  try {
    if (valor === null) sessionStorage.removeItem(clave);
    else sessionStorage.setItem(clave, valor);
  } catch {
    /* navegación privada */
  }
}

export function leerSesion(clave: string): string | null {
  try {
    return sessionStorage.getItem(clave);
  } catch {
    return null;
  }
}
