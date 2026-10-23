import dataclasses
import json
import logging

import httpx
import pytest

from pes import config as configuracion
from pes.alertas import Alertas
from pes.storage import StoreMemoria

pytestmark = pytest.mark.anyio

TOKEN = "123456:secreto-del-bot"


class Reloj:
    def __init__(self):
        self.t = 1_790_000_000.0  # 2026

    def __call__(self):
        return self.t


def _cfg(**extra):
    base = {"telegram_token": TOKEN, "telegram_chat": "42", "ntfy_tema": ""}
    return dataclasses.replace(configuracion.cargar(), **{**base, **extra})


def _registrar(pedidos, status=200):
    def handler(request):
        pedidos.append(request)
        return httpx.Response(status, json={"ok": status == 200})

    return handler


async def test_mismo_error_tres_veces_en_una_hora_avisa_una_vez():
    pedidos, reloj = [], Reloj()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_registrar(pedidos))) as cliente:
        alertas = Alertas(_cfg(), StoreMemoria(reloj), cliente, reloj)
        for _ in range(5):
            await alertas.error_nuestro("bloqueado", "instagram")
        assert len(pedidos) == 1
        for _ in range(3):  # otro error cuenta aparte
            await alertas.error_nuestro("ocupado", "tiktok")
        assert len(pedidos) == 2
        reloj.t += 3600  # la hora siguiente vuelve a contar
        for _ in range(3):
            await alertas.error_nuestro("bloqueado", "instagram")
        assert len(pedidos) == 3
    primero = pedidos[0]
    assert str(primero.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    cuerpo = json.loads(primero.content)
    assert cuerpo["chat_id"] == "42"
    assert "bloqueado" in cuerpo["text"] and "instagram" in cuerpo["text"] and "login" in cuerpo["text"]


async def test_cupo_lleno_una_vez_por_dia_de_cada_uno():
    pedidos, reloj = [], Reloj()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_registrar(pedidos))) as cliente:
        alertas = Alertas(_cfg(), StoreMemoria(reloj), cliente, reloj)
        await alertas.cupo_lleno("cupo_global")
        await alertas.cupo_lleno("cupo_global")
        await alertas.cupo_lleno("cupo_hora")
        assert len(pedidos) == 2
        reloj.t += 86400
        await alertas.cupo_lleno("cupo_global")
        assert len(pedidos) == 3
    assert "Pro" in json.loads(pedidos[0].content)["text"]


async def test_falta_config_una_vez_por_hora():
    pedidos, reloj = [], Reloj()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_registrar(pedidos))) as cliente:
        alertas = Alertas(_cfg(), StoreMemoria(reloj), cliente, reloj)
        for _ in range(3):
            await alertas.falta_config(["TURNSTILE_SECRET_KEY"])
    assert len(pedidos) == 1
    assert "TURNSTILE_SECRET_KEY" in json.loads(pedidos[0].content)["text"]


async def test_sin_canales_no_hace_nada():
    pedidos, reloj = [], Reloj()
    store = StoreMemoria(reloj)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_registrar(pedidos))) as cliente:
        alertas = Alertas(_cfg(telegram_token="", telegram_chat=""), store, cliente, reloj)
        assert not alertas.activas
        for _ in range(5):
            await alertas.error_nuestro("interno", "tiktok")
        await alertas.cupo_lleno("cupo_global")
        await alertas.falta_config(["CRON_SECRET"])
    assert pedidos == []


async def test_ntfy_y_los_dos_canales():
    pedidos, reloj = [], Reloj()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_registrar(pedidos))) as cliente:
        solo_ntfy = Alertas(_cfg(telegram_token="", ntfy_tema="pes-abc123"), StoreMemoria(reloj), cliente, reloj)
        assert await solo_ntfy.enviar("hola") is True
        assert str(pedidos[0].url) == "https://ntfy.sh/"
        cuerpo = json.loads(pedidos[0].content)
        assert cuerpo["topic"] == "pes-abc123" and cuerpo["message"].endswith("hola")
        assert cuerpo["title"] == "pasameloenespañol"
        ambos = Alertas(_cfg(ntfy_tema="pes-abc123"), StoreMemoria(reloj), cliente, reloj)
        assert await ambos.enviar("hola") is True
    assert len(pedidos) == 3


@pytest.mark.parametrize("falla", ["red", 500])
async def test_canal_caido_no_rompe_ni_loguea_el_token(caplog, falla):
    def handler(request):
        if falla == "red":
            raise httpx.ConnectError(f"no conecta a {request.url}")
        return httpx.Response(falla)

    reloj = Reloj()
    caplog.set_level(logging.DEBUG, logger="pes")  # httpx va en WARNING en app.py
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cliente:
        alertas = Alertas(_cfg(), StoreMemoria(reloj), cliente, reloj)
        assert await alertas.enviar("hola") is False
        for _ in range(3):
            await alertas.error_nuestro("interno", "tiktok")  # no levanta
    assert TOKEN not in caplog.text
    assert "telegram" in caplog.text
