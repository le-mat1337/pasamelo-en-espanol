"""Almacenamiento: estado de jobs y contadores en Redis (Upstash), videos en Cloudflare R2 o Vercel Blob.

Para tests y desarrollo local hay versiones en memoria y en disco. En producción
faltar Upstash o Blob es un error de configuración: nunca se cae a memoria.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from .errores import error
from .r2 import CredencialesR2, parsear_listado

log = logging.getLogger("pes")

RE_JOB_EN_PATH = re.compile(r"^videos/([A-Za-z0-9_-]{16})")


class Store(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def mget(self, keys: list[str]) -> list[str | None]: ...
    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool: ...
    async def incr(self, key: str) -> int: ...
    async def decr(self, key: str) -> int: ...
    async def expire(self, key: str, segundos: int) -> None: ...
    async def delete(self, *keys: str) -> None: ...


class StoreMemoria:
    """Redis de juguete para tests y `uvicorn` local. Un solo proceso, nada persiste."""

    def __init__(self, reloj: Callable[[], float] = time.time):
        self._datos: dict[str, tuple[str, float | None]] = {}
        self._reloj = reloj
        self.comandos = 0

    def _vivo(self, key: str) -> tuple[str, float | None] | None:
        item = self._datos.get(key)
        if item and item[1] is not None and item[1] <= self._reloj():
            del self._datos[key]
            return None
        return item

    async def get(self, key: str) -> str | None:
        self.comandos += 1
        item = self._vivo(key)
        return item[0] if item else None

    async def mget(self, keys: list[str]) -> list[str | None]:
        self.comandos += 1
        return [(i[0] if (i := self._vivo(k)) else None) for k in keys]

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        self.comandos += 1
        if nx and self._vivo(key):
            return False
        self._datos[key] = (value, self._reloj() + ex if ex else None)
        return True

    async def _sumar(self, key: str, delta: int) -> int:
        self.comandos += 1
        item = self._vivo(key)
        valor = int(item[0]) + delta if item else delta
        self._datos[key] = (str(valor), item[1] if item else None)
        return valor

    async def incr(self, key: str) -> int:
        return await self._sumar(key, 1)

    async def decr(self, key: str) -> int:
        return await self._sumar(key, -1)

    async def expire(self, key: str, segundos: int) -> None:
        self.comandos += 1
        if item := self._vivo(key):
            self._datos[key] = (item[0], self._reloj() + segundos)

    async def delete(self, *keys: str) -> None:
        self.comandos += 1
        for k in keys:
            self._datos.pop(k, None)

    def ttl(self, key: str) -> float | None:
        item = self._vivo(key)
        return None if not item or item[1] is None else item[1] - self._reloj()


class StoreUpstash:
    def __init__(self, url: str, token: str):
        from upstash_redis.asyncio import Redis

        self._r = Redis(url=url, token=token, allow_telemetry=False)

    async def get(self, key: str) -> str | None:
        return await self._r.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return list(await self._r.mget(*keys)) if keys else []

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        return bool(await self._r.set(key, value, ex=ex, nx=nx or None))

    async def incr(self, key: str) -> int:
        return int(await self._r.incr(key))

    async def decr(self, key: str) -> int:
        return int(await self._r.decr(key))

    async def expire(self, key: str, segundos: int) -> None:
        await self._r.expire(key, segundos)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._r.delete(*keys)


def crear_store(produccion: bool) -> Store:
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if url and token:
        return StoreUpstash(url, token)
    if produccion:
        raise error("config", http=503)
    return StoreMemoria()


class Videos(Protocol):
    async def subir(self, ruta: Path, job_id: str) -> str:
        """Sube el video y devuelve la referencia que se guarda en el job."""
        ...

    def url(self, ref: str, expira: float, ahora: float) -> str:
        """URL para el reproductor; no tiene que servir después de `expira`."""
        ...

    async def borrar_huerfanos(self, store: Store, ahora: float, vida_max_s: int) -> int: ...


async def _ids_vivos(store: Store, ids: list[str]) -> set[str]:
    vivos: set[str] = set()
    for i in range(0, len(ids), 100):  # MGET de a 100: un solo comando de Upstash por tanda
        tanda = ids[i : i + 100]
        valores = await store.mget([f"job:{j}" for j in tanda])
        vivos.update(j for j, v in zip(tanda, valores, strict=True) if v)
    return vivos


def _a_borrar(candidatos: list[tuple[str, str, float]], vivos: set[str], vida_max_s: int) -> list[str]:
    """Más viejo que la vida máxima, o con más de 1 h y sin job vivo (o que no es nuestro)."""
    return [ref for ref, jid, edad in candidatos if edad > vida_max_s or (edad > 3600 and jid not in vivos)]


class VideosR2:
    """Cloudflare R2 privado: transferencia gratis y URLs firmadas que vencen con el job.

    Un script que baje mil veces el mismo video no gasta nada (en Vercel Blob, en Hobby,
    agotaría la transferencia del mes y dejaría el sitio sin videos).
    """

    def __init__(self, cred: CredencialesR2, cliente: httpx.AsyncClient | None = None):
        self.cred = cred
        self._cliente = cliente or httpx.AsyncClient(timeout=60.0)

    def _firmar(self, metodo: str, clave: str, expira_s: int, ahora: float, params: dict | None = None) -> str:
        return self.cred.url(metodo, clave, expira_s, datetime.fromtimestamp(ahora, UTC), params)

    async def subir(self, ruta: Path, job_id: str) -> str:
        clave = f"videos/{job_id}.mp4"
        datos = await asyncio.to_thread(ruta.read_bytes)
        r = await self._cliente.put(
            self._firmar("PUT", clave, 600, time.time()),
            content=datos,
            headers={"Content-Type": "video/mp4", "Cache-Control": "private, max-age=3600"},
        )
        if r.status_code != 200:
            log.error("R2 no aceptó el video: HTTP %s", r.status_code)
            raise error("interno", http=502, devolver_cupo=True)
        return clave

    def url(self, ref: str, expira: float, ahora: float) -> str:
        return self._firmar("GET", ref, int(expira - ahora), ahora)

    async def borrar_huerfanos(self, store: Store, ahora: float, vida_max_s: int) -> int:
        candidatos: list[tuple[str, str, float]] = []
        token: str | None = None
        while True:  # ListObjectsV2 de a 1000: una operación de clase A por página
            params = {"list-type": "2", "prefix": "videos/", **({"continuation-token": token} if token else {})}
            r = await self._cliente.get(self._firmar("GET", "", 300, time.time(), params))
            if r.status_code != 200:
                log.error("R2 no dejó listar: HTTP %s", r.status_code)
                raise error("interno", http=502)
            objetos, token = parsear_listado(r.content)
            for o in objetos:
                m = RE_JOB_EN_PATH.match(o.clave)
                candidatos.append((o.clave, m.group(1) if m else "", ahora - o.modificado))
            if not token:
                break
        vivos = await _ids_vivos(store, [c[1] for c in candidatos if c[1]])
        borrar = _a_borrar(candidatos, vivos, vida_max_s)
        for clave in borrar:  # DeleteObject es gratis en R2
            r = await self._cliente.delete(self._firmar("DELETE", clave, 300, time.time()))
            if r.status_code not in (200, 204, 404):
                log.error("R2 no dejó borrar un video: HTTP %s", r.status_code)
        return len(borrar)


class VideosBlob:
    """Vercel Blob público. La URL lleva un sufijo aleatorio: no se puede adivinar ni listar."""

    async def subir(self, ruta: Path, job_id: str) -> str:
        from vercel import blob

        datos = await asyncio.to_thread(ruta.read_bytes)
        resultado = await blob.put_blob(
            f"videos/{job_id}.mp4",
            datos,
            access="public",
            content_type="video/mp4",
            add_random_suffix=True,
            cache_control_max_age=3600,
        )
        return resultado.url

    def url(self, ref: str, expira: float, ahora: float) -> str:
        return ref  # pública con sufijo aleatorio; el cron la borra cuando vence el job

    async def borrar_huerfanos(self, store: Store, ahora: float, vida_max_s: int) -> int:
        """Borra los videos cuyo job ya expiró (o que superan la vida máxima)."""
        from vercel import blob

        candidatos: list[tuple[str, str, float]] = []
        async for item in blob.query_blobs(prefix="videos/"):
            edad = ahora - item.uploaded_at.timestamp()
            if m := RE_JOB_EN_PATH.match(item.pathname):
                candidatos.append((item.url, m.group(1), edad))
            elif edad > 3600:
                candidatos.append((item.url, "", edad))  # algo que no subimos nosotros
        vivos = await _ids_vivos(store, [c[1] for c in candidatos if c[1]])
        borrar = _a_borrar(candidatos, vivos, vida_max_s)
        for i in range(0, len(borrar), 1000):
            await blob.delete_blob(borrar[i : i + 1000])
        return len(borrar)


class VideosLocales:
    """Solo desarrollo: guarda en disco y sirve desde /api/dev/videos/<archivo>."""

    def __init__(self, carpeta: Path):
        self.carpeta = carpeta
        self.carpeta.mkdir(parents=True, exist_ok=True)

    async def subir(self, ruta: Path, job_id: str) -> str:
        destino = self.carpeta / f"{job_id}.mp4"
        await asyncio.to_thread(shutil.copyfile, ruta, destino)
        return f"/api/dev/videos/{destino.name}"

    def url(self, ref: str, expira: float, ahora: float) -> str:
        return ref

    async def borrar_huerfanos(self, store: Store, ahora: float, vida_max_s: int) -> int:
        archivos = list(self.carpeta.glob("*.mp4"))
        vivos = await _ids_vivos(store, [a.stem for a in archivos])
        borrados = 0
        for a in archivos:
            edad = ahora - a.stat().st_mtime
            if edad > vida_max_s or (a.stem not in vivos and edad > 60):
                a.unlink(missing_ok=True)
                borrados += 1
        return borrados


def crear_videos(produccion: bool) -> Videos:
    if cred := CredencialesR2.desde_env():
        return VideosR2(cred)
    if os.environ.get("BLOB_READ_WRITE_TOKEN") or os.environ.get("BLOB_STORE_ID"):
        return VideosBlob()
    if produccion:
        raise error("config", http=503)
    return VideosLocales(Path(os.environ.get("PES_CARPETA_LOCAL", "/tmp/pes-videos")))  # noqa: S108


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()
