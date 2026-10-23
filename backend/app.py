"""API de pasameloenespañol.com (FastAPI). Vercel la sirve como service en /api/*."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from pes import config as configuracion
from pes.descarga import descargar
from pes.errores import PesError, error
from pes.jobs import (
    Deps,
    clave_video,
    guardar,
    leer,
    nuevo_id,
    nuevo_job,
    procesar,
    publico,
)
from pes.limites import fecha_local, hash_ip, ip_cliente, liberar, reservar, verificar_turnstile
from pes.storage import VideosLocales, crear_store, crear_videos
from pes.traduce import crear_traductor
from pes.transcribe import transcribir_groq
from pes.urls import UA, resolver

configuracion.cargar_env_local()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
for ruidoso in ("httpx", "httpcore"):  # en INFO loguean cada URL: las firmadas de R2 llevan credenciales
    logging.getLogger(ruidoso).setLevel(logging.WARNING)
log = logging.getLogger("pes")

app = FastAPI(title="pasameloenespañol.com", docs_url=None, redoc_url=None, openapi_url=None)

MAX_CUERPO = 8 * 1024
LATIDO_S = 10.0
_TAREAS: set[asyncio.Task] = set()
_DEPS: Deps | None = None


def _wait_until(tarea: asyncio.Task) -> None:
    """En Vercel mantiene viva la invocación hasta que el job termina, aunque el celu corte."""
    try:
        from vercel.functions import wait_until

        wait_until(tarea)
    except Exception as e:  # noqa: BLE001 - fuera de Vercel no hace falta
        log.debug("wait_until no disponible: %s", e)


def obtener_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        cfg = configuracion.cargar()
        cliente = httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20.0)
        store = crear_store(cfg.produccion)
        _DEPS = Deps(
            cfg=cfg,
            store=store,
            videos=crear_videos(cfg.produccion),
            resolver=partial(resolver, cliente=httpx.AsyncClient(timeout=8.0, headers={"User-Agent": UA})),
            verificar_humano=lambda token, ip: verificar_turnstile(token, ip, cfg, cliente),
            descargar=descargar,
            transcribir=lambda audio: transcribir_groq(audio, cfg.groq_api_key, cfg.modelo_groq_whisper, cliente),
            traducir=crear_traductor(cfg, cliente),
        )
        if cfg.demo:
            from pes import demo

            log.warning("MODO DEMO: subtítulos inventados, sin Groq")
            _DEPS.transcribir = demo.transcribir
            _DEPS.traducir = demo.traducir
    return _DEPS


DepsDep = Annotated[Deps, Depends(obtener_deps)]


@app.exception_handler(PesError)
async def _pes_error(_: Request, e: PesError) -> JSONResponse:
    return JSONResponse(e.a_dict(), status_code=e.http, headers={"Cache-Control": "no-store"})


@app.exception_handler(RequestValidationError)
async def _validacion(_: Request, __: RequestValidationError) -> JSONResponse:
    return JSONResponse(error("link_invalido").a_dict(), status_code=400)


@app.exception_handler(Exception)
async def _inesperado(_: Request, e: Exception) -> JSONResponse:
    log.exception("error inesperado: %s", type(e).__name__)
    return JSONResponse(error("interno").a_dict(), status_code=500)


@app.middleware("http")
async def _cabeceras(request: Request, siguiente):
    largo = request.headers.get("content-length")
    if largo and (not largo.isdigit() or int(largo) > MAX_CUERPO):
        return JSONResponse(error("link_invalido").a_dict(), status_code=413)
    respuesta = await siguiente(request)
    respuesta.headers.setdefault("Cache-Control", "no-store")
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    respuesta.headers["X-Robots-Tag"] = "noindex"
    return respuesta


class PedidoJob(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    turnstile: str | None = Field(default=None, max_length=2048)


def _linea(evento: dict) -> bytes:
    return (json.dumps(evento, ensure_ascii=False) + "\n").encode()


@app.get("/api/salud")
async def salud(deps: DepsDep) -> dict:
    return {"ok": True, "pausado": deps.cfg.pausado, "configurado": not deps.cfg.faltantes()}


@app.post("/api/jobs")
async def crear_job(pedido: PedidoJob, request: Request, deps: DepsDep) -> Response:
    cfg, store = deps.cfg, deps.store
    if cfg.pausado:
        raise error("pausado", http=503)
    if faltan := cfg.faltantes():
        log.error("faltan variables de entorno: %s", ", ".join(faltan))
        raise error("config", http=503)

    ref = await deps.resolver(pedido.url)  # valida antes de gastar nada
    ip = ip_cliente(request.headers, request.client.host if request.client else None)
    await deps.verificar_humano(pedido.turnstile, ip)
    ahora = deps.reloj()

    # Mismo video ya pedido: se devuelve el mismo link, sin gastar ni consumir cupo.
    if existente := await store.get(clave_video(ref)):
        job = await leer(store, existente)
        if job and job.get("estado") != "error":
            if job["estado"] == "listo":
                nueva = min(job["creado"] + cfg.vida_max_horas * 3600, ahora + cfg.ttl_s)
                if nueva > job["expira"]:
                    job["expira"] = nueva
                    await guardar(store, job, ahora)
                    await store.set(clave_video(ref), job["id"], ex=max(60, int(nueva - ahora)))
            return JSONResponse({"etapa": job["estado"], "id": job["id"], "url": f"/v/{job['id']}"})
        await store.delete(clave_video(ref))  # apuntaba a un job que falló o venció: se reintenta

    reserva = await reservar(store, hash_ip(ip, cfg.sal, fecha_local(ahora)), cfg, ahora)
    job_id = nuevo_id()
    if not await store.set(clave_video(ref), job_id, ex=cfg.ttl_s, nx=True):
        # Otra persona lo pidió en este mismo instante: se espera ese.
        await liberar(store, reserva, devolver_cupo=True)
        otro = await store.get(clave_video(ref))
        return JSONResponse({"etapa": "procesando", "id": otro, "url": f"/v/{otro}"})

    job = nuevo_job(job_id, ref, ahora, cfg.ttl_s)
    await guardar(store, job, ahora)

    cola: asyncio.Queue[dict] = asyncio.Queue()
    tarea = asyncio.create_task(procesar(job, ref, reserva, deps, cola.put_nowait))
    _TAREAS.add(tarea)
    tarea.add_done_callback(_TAREAS.discard)
    _wait_until(tarea)

    async def eventos() -> AsyncIterator[bytes]:
        yield _linea({"etapa": "aceptado", "id": job_id})
        while True:
            try:
                evento = await asyncio.wait_for(cola.get(), timeout=LATIDO_S)
            except TimeoutError:
                yield _linea({"etapa": "latido"})
                continue
            yield _linea(evento)
            if evento["etapa"] in ("listo", "error"):
                return

    return StreamingResponse(
        eventos(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}")
async def ver_job(job_id: str, deps: DepsDep) -> JSONResponse:
    job = await leer(deps.store, job_id)
    if not job:
        raise error("no_encontrado", http=404)
    ahora = deps.reloj()
    datos = publico(job, ahora)
    cabeceras = {"Cache-Control": "no-store"}
    if datos.get("estado") == "listo":
        datos["video_url"] = deps.videos.url(job["video"], job["expira"], ahora)
        restante = int(job["expira"] - ahora)
        cabeceras["Cache-Control"] = f"public, max-age=60, s-maxage={max(0, min(3600, restante))}"
    return JSONResponse(datos, headers=cabeceras)


@app.get("/api/cron/limpiar")
async def limpiar(request: Request, deps: DepsDep) -> dict:
    esperado = f"Bearer {deps.cfg.cron_secret}"
    recibido = request.headers.get("authorization", "")
    if not deps.cfg.cron_secret or not hmac.compare_digest(recibido.encode(), esperado.encode()):
        raise PesError("no_autorizado", "No.", http=401)
    borrados = await deps.videos.borrar_huerfanos(deps.store, deps.reloj(), deps.cfg.vida_max_horas * 3600)
    log.info("limpieza: %d videos borrados", borrados)
    return {"borrados": borrados}


@app.get("/api/dev/videos/{nombre}")
async def video_local(nombre: str, deps: DepsDep) -> FileResponse:
    """Solo en desarrollo, cuando no hay Blob configurado."""
    if deps.cfg.produccion or not isinstance(deps.videos, VideosLocales):
        raise error("no_encontrado", http=404)
    ruta = (deps.videos.carpeta / Path(nombre).name).resolve()
    if ruta.parent != deps.videos.carpeta.resolve() or ruta.suffix != ".mp4" or not ruta.is_file():
        raise error("no_encontrado", http=404)
    return FileResponse(ruta, media_type="video/mp4")
