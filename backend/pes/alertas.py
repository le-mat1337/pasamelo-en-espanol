"""Alertas al celu cuando algo se rompe en producción: Telegram (un bot) y/o ntfy (app gratis, sin cuenta).

Cuándo se avisa (deduplicado en Redis para no llenar el celu):
- El mismo error "nuestro" (los que devuelven el cupo: Instagram/TikTok bloqueando, Groq sin cupo,
  algo roto) 3 veces en la misma hora: un aviso por error y por hora.
- Se llenó el cupo global del día o de la hora: un aviso por día de cada uno (señal de pasar a Pro).
- Falta configuración en producción (no se procesa nada): un aviso por hora.

Nunca frenan ni rompen un pedido: timeout corto y cualquier falla se loguea sin el token y sigue.
Los avisos no llevan datos de nadie: solo el código de error, la plataforma y una pista.

Para configurarlo: `uv run python -m pes.alertas --chat` (con el token en backend/.env) muestra el
chat_id después de mandarle un mensaje al bot, y `--probar` manda un aviso de prueba.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .config import Config
from .limites import fecha_local, hora_local
from .storage import Store

log = logging.getLogger("pes")

TELEGRAM = "https://api.telegram.org/bot{token}/{metodo}"
NTFY = "https://ntfy.sh/"  # publicación por JSON: las cabeceras no admiten la ñ
REPETICIONES = 3  # el mismo error nuestro, tantas veces en la misma hora
TIMEOUT_S = 5.0
PISTAS = {
    "bloqueado": "Instagram o TikTok no dejan bajar: puede ser que pidan login o que bloqueen la IP de Vercel.",
    "ig_vacio": 'Instagram devuelve vacío. Si sigue, poner en la home "por ahora TikTok".',
    "ocupado": "Groq sin cupo o muy lento (429). Mirar console.groq.com.",
    "traduccion": "El traductor devuelve cualquier cosa.",
    "interno": "Se rompió algo: vercel logs --environment production --level error",
}


@dataclass
class Alertas:
    cfg: Config
    store: Store
    cliente: httpx.AsyncClient
    reloj: Callable[[], float] = field(default=time.time)

    @property
    def activas(self) -> bool:
        return bool((self.cfg.telegram_token and self.cfg.telegram_chat) or self.cfg.ntfy_tema)

    async def error_nuestro(self, codigo: str, plataforma: str) -> None:
        if not self.activas:
            return
        try:
            clave = f"alerta:error:{codigo}:{hora_local(self.reloj())}"
            n = await self.store.incr(clave)
            if n == 1:
                await self.store.expire(clave, 3700)
        except Exception as e:  # noqa: BLE001 - sin Redis no se deduplica: mejor no avisar que spamear
            log.warning("alertas: no se pudo contar el error (%s)", type(e).__name__)
            return
        if n == REPETICIONES:
            pista = PISTAS.get(codigo, "")
            await self.enviar(f"🚨 «{codigo}» {n} veces en esta hora (el último en {plataforma}). {pista}")

    async def cupo_lleno(self, codigo: str) -> None:
        if codigo == "cupo_global":
            texto = (
                f"📈 Se llenó el cupo del día ({self.cfg.max_jobs_dia} videos): el resto ve «Hoy ya se llenó». "
                "Si pasa seguido, pasar a Pro y subir PES_MAX_JOBS_DIA."
            )
        else:
            texto = (
                f"📈 Se llenó el cupo de la hora ({self.cfg.max_jobs_hora} videos): hay más gente que capacidad. "
                "Se puede subir PES_MAX_JOBS_HORA si la CPU del mes da."
            )
        await self._una_vez(f"alerta:{codigo}:{fecha_local(self.reloj())}", 90000, texto)

    async def falta_config(self, faltan: list[str]) -> None:
        texto = f"⚙️ Falta configuración en producción ({', '.join(faltan)}): la página no procesa nada."
        await self._una_vez("alerta:config", 3600, texto)

    async def _una_vez(self, clave: str, ttl: int, texto: str) -> None:
        if not self.activas:
            return
        try:
            primera = await self.store.set(clave, "1", ex=ttl, nx=True)
        except Exception as e:  # noqa: BLE001
            log.warning("alertas: no se pudo deduplicar (%s)", type(e).__name__)
            return
        if primera:
            await self.enviar(texto)

    async def enviar(self, texto: str) -> bool:
        """Manda el aviso por los canales configurados. True si llegó por alguno. Nunca levanta."""
        texto = f"pasameloenespañol: {texto}"
        envios = []
        if self.cfg.telegram_token and self.cfg.telegram_chat:
            envios.append(self._telegram(texto))
        if self.cfg.ntfy_tema:
            envios.append(self._ntfy(texto))
        resultados = await asyncio.gather(*envios, return_exceptions=True)
        return any(r is True for r in resultados)

    async def _telegram(self, texto: str) -> bool:
        url = TELEGRAM.format(token=self.cfg.telegram_token, metodo="sendMessage")
        datos = {"chat_id": self.cfg.telegram_chat, "text": texto, "disable_web_page_preview": True}
        return await self._post("telegram", url, json=datos)

    async def _ntfy(self, texto: str) -> bool:
        datos = {"topic": self.cfg.ntfy_tema, "title": "pasameloenespañol", "message": texto, "priority": 4,
                 "tags": ["rotating_light"]}  # fmt: skip
        return await self._post("ntfy", NTFY, json=datos)

    async def _post(self, canal: str, url: str, **kwargs) -> bool:
        try:
            resp = await self.cliente.post(url, timeout=TIMEOUT_S, **kwargs)
        except Exception as e:  # noqa: BLE001 - el mensaje de httpx trae la URL, y la de Telegram lleva el token
            log.warning("alertas: %s no respondió (%s)", canal, type(e).__name__)
            return False
        if resp.status_code != 200:
            log.warning("alertas: %s respondió %s", canal, resp.status_code)
            return False
        return True


async def _principal(args: argparse.Namespace) -> int:
    from . import config as configuracion

    configuracion.cargar_env_local()
    cfg = configuracion.cargar()
    async with httpx.AsyncClient() as cliente:
        if args.chat:
            if not cfg.telegram_token:
                print("Falta PES_TELEGRAM_TOKEN en backend/.env", file=sys.stderr)
                return 2
            url = TELEGRAM.format(token=cfg.telegram_token, metodo="getUpdates")
            resp = await cliente.get(url, timeout=10)
            if resp.status_code != 200:
                print(f"Telegram respondió {resp.status_code}: ¿el token está bien?", file=sys.stderr)
                return 1
            chats = {}
            for u in resp.json().get("result", []):
                chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
                if "id" in chat:
                    chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
            if not chats:
                print("No hay mensajes: mandale cualquier cosa al bot desde Telegram y volvé a correr esto.")
            for chat_id, nombre in chats.items():
                print(f"PES_TELEGRAM_CHAT={chat_id}   ({nombre})")
            return 0
        alertas = Alertas(cfg, store=None, cliente=cliente)  # type: ignore[arg-type] - enviar no usa Redis
        if not alertas.activas:
            print("No hay ningún canal configurado (PES_TELEGRAM_TOKEN + PES_TELEGRAM_CHAT, o PES_NTFY_TEMA).")
            return 2
        ok = await alertas.enviar("✅ aviso de prueba. Si te llegó, las alertas andan.")
        print("Enviado." if ok else "No llegó por ningún canal: mirá el log de arriba.")
        return 0 if ok else 1


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="pes.alertas", description="Configurar y probar las alertas al celu.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--chat", action="store_true", help="mostrar el chat_id de quien le escribió al bot")
    g.add_argument("--probar", action="store_true", help="mandar un aviso de prueba por los canales configurados")
    sys.exit(asyncio.run(_principal(p.parse_args())))


if __name__ == "__main__":
    main()
