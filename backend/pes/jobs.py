"""Un job = un video pedido. Estado en Redis, pipeline de punta a punta y eventos de progreso."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .cues import Cue, armar_cues, partir_para_traducir
from .descarga import Descarga
from .errores import PesError, error
from .limites import Reserva, liberar
from .storage import Store, Videos
from .texto import TextoVisto
from .traduce import Traductor, limpiar
from .transcribe import Transcripcion
from .urls import VideoRef

log = logging.getLogger("pes")

RE_ID = re.compile(r"^[A-Za-z0-9_-]{16}$")
CORTADO_S = 360  # un job "procesando" sin novedades hace 6 minutos se dio por muerto
# La URL del video no se guarda: la arma la API al leer (en R2 es firmada y vence con el job).
CAMPOS_PUBLICOS = (
    "id", "estado", "etapa", "plataforma", "original", "autor", "duracion",
    "cues", "texto", "caption", "cancion", "expira", "codigo", "mensaje",
)  # fmt: skip

Emitir = Callable[[dict], None]


@dataclass
class Deps:
    cfg: Config
    store: Store
    videos: Videos
    resolver: Callable[[str], Awaitable[VideoRef]]
    verificar_humano: Callable[[str | None, str], Awaitable[None]]
    descargar: Callable[[VideoRef, Path, int, int], Descarga]
    transcribir: Callable[[Path], Awaitable[Transcripcion]]
    traducir: Traductor
    reloj: Callable[[], float] = field(default=time.time)
    # (video, duración) -> texto escrito sobre el video. None: no se lee.
    leer_texto: Callable[[Path, float], Awaitable[list[TextoVisto]]] | None = None


def nuevo_id() -> str:
    return secrets.token_urlsafe(12)  # 16 caracteres, 96 bits


def clave_job(job_id: str) -> str:
    return f"job:{job_id}"


def clave_video(ref: VideoRef) -> str:
    return f"video:{ref.clave}"


def nuevo_job(job_id: str, ref: VideoRef, ahora: float, ttl_s: int) -> dict:
    return {
        "id": job_id,
        "estado": "procesando",
        "etapa": "aceptado",
        "plataforma": ref.plataforma,
        "video_id": ref.id,
        "original": ref.url,
        "creado": ahora,
        "actualizado": ahora,
        "expira": ahora + ttl_s,
    }


async def guardar(store: Store, job: dict, ahora: float) -> None:
    ttl = max(60, int(job["expira"] - ahora))
    await store.set(clave_job(job["id"]), json.dumps(job, ensure_ascii=False), ex=ttl)


async def leer(store: Store, job_id: str) -> dict | None:
    if not RE_ID.match(job_id or ""):
        return None
    crudo = await store.get(clave_job(job_id))
    if not crudo:
        return None
    try:
        return json.loads(crudo)
    except json.JSONDecodeError:
        return None


def publico(job: dict, ahora: float) -> dict:
    if job.get("estado") == "procesando" and ahora - float(job.get("actualizado", 0)) > CORTADO_S:
        e = error("cortado")
        job = {**job, "estado": "error", "etapa": "error", **e.a_dict()}
    return {k: job[k] for k in CAMPOS_PUBLICOS if k in job}


async def procesar(job: dict, ref: VideoRef, reserva: Reserva, deps: Deps, emitir: Emitir) -> None:
    """Corre el pipeline completo. Nunca levanta: todo termina en 'listo' o 'error'."""
    carpeta = Path(tempfile.mkdtemp(prefix="pes-"))
    devolver_cupo = False
    try:
        async with asyncio.timeout(deps.cfg.limite_job_s):
            await _pipeline(job, ref, deps, carpeta, emitir)
    except Exception as e:  # noqa: BLE001 - el pipeline siempre termina en un estado
        if isinstance(e, PesError):
            pes = e
        elif isinstance(e, TimeoutError):
            log.warning("job vencido id=%s %s", job["id"], ref.clave)
            pes = error("interno", http=504, devolver_cupo=True)
        else:
            log.exception("job roto id=%s %s", job["id"], ref.clave)
            pes = error("interno", http=500, devolver_cupo=True)
        devolver_cupo = pes.devolver_cupo
        await _fallar(job, ref, pes, deps, emitir)
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)
        try:
            await liberar(deps.store, reserva, devolver_cupo)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo liberar la reserva del job %s", job["id"])


async def _fallar(job: dict, ref: VideoRef, pes: PesError, deps: Deps, emitir: Emitir) -> None:
    log.info("job error id=%s %s codigo=%s", job["id"], ref.clave, pes.codigo)
    job.update(estado="error", etapa="error", actualizado=deps.reloj(), **pes.a_dict())
    try:
        await guardar(deps.store, job, deps.reloj())
        await deps.store.delete(clave_video(ref))  # que el próximo pedido del mismo video reintente
    except Exception:  # noqa: BLE001
        log.exception("no se pudo guardar el error del job %s", job["id"])
    emitir({"etapa": "error", "id": job["id"], **pes.a_dict()})


async def _pipeline(job: dict, ref: VideoRef, deps: Deps, carpeta: Path, emitir: Emitir) -> None:
    cfg, store = deps.cfg, deps.store
    inicio = deps.reloj()

    async def etapa(nombre: str) -> None:
        job.update(etapa=nombre, actualizado=deps.reloj())
        await guardar(store, job, deps.reloj())
        emitir({"etapa": nombre, "id": job["id"]})

    await etapa("descargando")
    bajado = await asyncio.to_thread(deps.descargar, ref, carpeta, cfg.max_mb, cfg.max_duracion_s)

    await etapa("escuchando")
    transcripcion = await deps.transcribir(bajado.audio)
    hay_voz = transcripcion.cantidad_palabras >= 3
    puede_leer = cfg.texto and deps.leer_texto is not None
    if not hay_voz and not puede_leer:
        raise error("sin_voz", http=422)
    if hay_voz and not transcripcion.es_ingles:
        raise error("no_ingles", http=422)

    cues: list[Cue] = []
    caption: str | None = None
    cancion = bajado.cancion
    if hay_voz:
        await etapa("traduciendo")
        bloques = partir_para_traducir(transcripcion.segmentos)
        extra = [bajado.caption] if bajado.caption else []
        tr = await deps.traducir(
            [b.texto for b in bloques] + extra, tolerantes=frozenset(range(len(bloques), len(bloques) + len(extra)))
        )
        cues = armar_cues([(b.ini, b.fin, t) for b, t in zip(bloques, tr.textos[: len(bloques)], strict=True)])
        if bajado.caption:
            caption = _traducido(bajado.caption, tr.textos[-1])
        cancion = cancion or tr.cancion

    # Canción, video mudo o casi (menos de una palabra cada 2 s, como un reel con música y una
    # frase suelta): el texto escrito encima suele ser lo que hay que entender.
    poca_voz = transcripcion.cantidad_palabras < bajado.duracion / 2
    texto: list[Cue] = []
    if puede_leer and (not hay_voz or cancion or poca_voz):
        await etapa("leyendo")
        try:
            texto, caption_leido = await _leer_texto(bajado, deps, con_caption=not hay_voz)
            caption = caption or caption_leido
        except Exception as e:  # noqa: BLE001 - con voz, el texto es un extra: el video sale igual
            if not hay_voz:
                raise
            motivo = e.codigo if isinstance(e, PesError) else type(e).__name__
            log.warning("job %s sale sin el texto escrito: %s", job["id"], motivo)
    if not cues and not texto:
        raise error("sin_voz", http=422)

    await etapa("guardando")
    video = await deps.videos.subir(bajado.video, job["id"])
    job.update(
        estado="listo",
        etapa="listo",
        video=video,
        cues=[c.a_dict() for c in cues],
        cancion=cancion,
        autor=bajado.autor,
        duracion=round(bajado.duracion, 1),
        idioma=transcripcion.idioma,
        actualizado=deps.reloj(),
    )
    if texto:
        job["texto"] = [c.a_dict() for c in texto]
    if caption:
        job["caption"] = caption
    await guardar(store, job, deps.reloj())
    emitir({"etapa": "listo", "id": job["id"], "url": f"/v/{job['id']}"})
    log.info(
        "job listo id=%s %s %.1fs en %.0fs (%d subtítulos, %d textos)",
        job["id"], ref.clave, bajado.duracion, deps.reloj() - inicio, len(cues), len(texto),
    )  # fmt: skip


def _traducido(original: str, traducido: str) -> str | None:
    """Un texto tolerante que volvió igual (ya estaba en castellano, era un nombre) no se muestra."""
    t = traducido.strip()
    return t if t and t.casefold() != limpiar(original).casefold() else None


async def _leer_texto(bajado: Descarga, deps: Deps, con_caption: bool) -> tuple[list[Cue], str | None]:
    """Lee el texto escrito, traduce el que está en inglés (y el caption, si todavía no se tradujo)."""
    if deps.leer_texto is None:
        return [], None
    vistos = [v for v in await deps.leer_texto(bajado.video, bajado.duracion) if v.en]
    if not vistos:
        return [], None
    originales = [v.texto for v in vistos] + ([bajado.caption] if con_caption and bajado.caption else [])
    tr = await deps.traducir(originales, tolerantes=frozenset(range(len(originales))))
    cues: list[Cue] = []
    for v, t in zip(vistos, tr.textos, strict=False):
        if traducido := _traducido(v.texto, t):
            cues.append(Cue(v.ini, v.fin, traducido, v.pos))
    caption = None
    if len(originales) > len(vistos):
        caption = _traducido(originales[-1], tr.textos[-1])
    return cues, caption
