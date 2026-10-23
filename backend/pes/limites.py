"""Límites de uso y captcha. Es lo que mantiene el costo en cero aunque el repo sea público.

Capas: WAF de Vercel -> Turnstile -> 1 job en curso por IP ->
3 por día por IP -> tope global por hora y por día. Las IPs no se guardan: se usa un hash
con una sal que cambia cada día, y las IPv6 se agrupan por /64 (rotar dentro de un /64 es gratis).
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .errores import error
from .storage import Store

log = logging.getLogger("pes")
ZONA = ZoneInfo("America/Argentina/Buenos_Aires")
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
ACCION_TURNSTILE = "pasamelo"
LOCK_S = 300


def ip_cliente(headers: Mapping[str, str], respaldo: str | None) -> str:
    """En Vercel, x-vercel-forwarded-for / x-real-ip / x-forwarded-for los pone la plataforma
    y pisa lo que mande el cliente, así que son confiables. Fuera de Vercel no lo son."""
    for nombre in ("x-vercel-forwarded-for", "x-real-ip", "x-forwarded-for"):
        if valor := headers.get(nombre):
            return valor.split(",")[0].strip()
    return respaldo or "desconocida"


def normalizar_ip(ip: str) -> str:
    try:
        direccion = ipaddress.ip_address(ip.strip())
    except ValueError:
        return "desconocida"
    if isinstance(direccion, ipaddress.IPv6Address):
        if direccion.ipv4_mapped:
            return str(direccion.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{direccion}/64", strict=False))
    return str(direccion)


def fecha_local(ahora: float) -> str:
    return datetime.fromtimestamp(ahora, ZONA).strftime("%Y-%m-%d")


def hora_local(ahora: float) -> str:
    return datetime.fromtimestamp(ahora, ZONA).strftime("%Y-%m-%dT%H")


def segundos_hasta_medianoche(ahora: float) -> int:
    dt = datetime.fromtimestamp(ahora, ZONA)
    manana = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((manana - dt).total_seconds()) + 60


def hash_ip(ip: str, sal: str, fecha: str) -> str:
    return hashlib.sha256(f"{sal}|{fecha}|{normalizar_ip(ip)}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Reserva:
    ip_hash: str
    claves: tuple[str, ...]  # contadores incrementados (ip del día, global del día, global de la hora)

    @property
    def lock(self) -> str:
        return f"rl:activo:{self.ip_hash}"


async def reservar(store: Store, ip_hash: str, cfg: Config, ahora: float) -> Reserva:
    lock = f"rl:activo:{ip_hash}"
    if not await store.set(lock, "1", ex=LOCK_S, nx=True):
        raise error("en_curso", http=429)

    fecha, hora = fecha_local(ahora), hora_local(ahora)
    topes = (
        (f"rl:ip:{fecha}:{ip_hash}", cfg.max_por_ip_dia, segundos_hasta_medianoche(ahora), "cupo_ip"),
        (f"rl:dia:{fecha}", cfg.max_jobs_dia, segundos_hasta_medianoche(ahora), "cupo_global"),
        (f"rl:hora:{hora}", cfg.max_jobs_hora, 3700, "cupo_hora"),
    )
    sumadas: list[str] = []
    try:
        for clave, tope, ttl, codigo in topes:
            n = await store.incr(clave)
            sumadas.append(clave)
            if n == 1:
                await store.expire(clave, ttl)
            if n > tope:
                raise error(codigo, http=429)
    except BaseException:
        for clave in sumadas:
            await store.decr(clave)
        await store.delete(lock)
        raise
    return Reserva(ip_hash, tuple(sumadas))


async def liberar(store: Store, reserva: Reserva, devolver_cupo: bool) -> None:
    await store.delete(reserva.lock)
    if devolver_cupo:
        for clave in reserva.claves:
            await store.decr(clave)


async def verificar_turnstile(token: str | None, ip: str, cfg: Config, cliente: httpx.AsyncClient) -> None:
    if not cfg.turnstile_secret:
        if cfg.produccion:
            raise error("config", http=503)  # nunca se apaga el captcha en producción por olvido
        return  # desarrollo local
    if not token or len(token) > 2048:
        raise error("captcha", http=403)
    try:
        resp = await cliente.post(
            SITEVERIFY,
            data={"secret": cfg.turnstile_secret, "response": token, "remoteip": ip},
            timeout=8.0,
        )
        datos = resp.json()
    except (httpx.HTTPError, ValueError):
        raise error("captcha", http=503) from None
    if not datos.get("success"):
        raise error("captcha", http=403)
    if (datos.get("metadata") or {}).get("result_with_testing_key"):
        # Claves de prueba de Cloudflare (siempre pasan, sin action ni hostname reales): solo en previews
        if cfg.entorno == "production":
            log.error("turnstile: clave de prueba en producción")
            raise error("captcha", http=403)
        return
    if datos.get("action") != ACCION_TURNSTILE:
        log.warning("turnstile: action inesperada %r", datos.get("action"))
        raise error("captcha", http=403)
    if cfg.hosts_turnstile and str(datos.get("hostname", "")).lower() not in cfg.hosts_turnstile:
        log.warning("turnstile: hostname inesperado %r", datos.get("hostname"))
        raise error("captcha", http=403)
