import dataclasses

import httpx
import pytest

from pes import config as configuracion
from pes.errores import PesError
from pes.limites import hash_ip, ip_cliente, normalizar_ip, verificar_turnstile

pytestmark = pytest.mark.anyio


def _cfg(**kw):
    base = dict(turnstile_secret="secreto", entorno="production", hosts_turnstile=("xn--pasameloenespaol-kub.com",))
    base.update(kw)
    return dataclasses.replace(configuracion.cargar(), **base)


def _cliente(respuesta: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"remoteip=1.2.3.4" in request.content
        return httpx.Response(200, json=respuesta)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "respuesta,ok",
    [
        ({"success": True, "action": "pasamelo", "hostname": "xn--pasameloenespaol-kub.com"}, True),
        ({"success": False}, False),
        ({"success": True, "action": "otra", "hostname": "xn--pasameloenespaol-kub.com"}, False),
        ({"success": True, "hostname": "xn--pasameloenespaol-kub.com"}, False),
        ({"success": True, "action": "pasamelo", "hostname": "evil.com"}, False),
    ],
)
async def test_turnstile(respuesta, ok):
    async with _cliente(respuesta) as c:
        if ok:
            await verificar_turnstile("tok", "1.2.3.4", _cfg(), c)
        else:
            with pytest.raises(PesError):
                await verificar_turnstile("tok", "1.2.3.4", _cfg(), c)


PRUEBA = {"success": True, "hostname": "example.com", "metadata": {"result_with_testing_key": True}}


async def test_turnstile_clave_de_prueba_solo_fuera_de_produccion():
    async with _cliente(PRUEBA) as c:
        await verificar_turnstile("tok", "1.2.3.4", _cfg(entorno="preview"), c)
        with pytest.raises(PesError):
            await verificar_turnstile("tok", "1.2.3.4", _cfg(entorno="production"), c)


async def test_turnstile_sin_token():
    async with _cliente({}) as c:
        with pytest.raises(PesError) as e:
            await verificar_turnstile(None, "1.2.3.4", _cfg(), c)
    assert e.value.codigo == "captcha"


async def test_turnstile_sin_secreto_en_produccion_falla_cerrado():
    async with _cliente({}) as c:
        with pytest.raises(PesError) as e:
            await verificar_turnstile("tok", "1.2.3.4", _cfg(turnstile_secret=""), c)
    assert e.value.codigo == "config"


def test_ip_cliente_prefiere_cabeceras_de_vercel():
    assert ip_cliente({"x-vercel-forwarded-for": "1.1.1.1", "x-forwarded-for": "2.2.2.2"}, "3.3.3.3") == "1.1.1.1"
    assert ip_cliente({"x-forwarded-for": "2.2.2.2, 9.9.9.9"}, None) == "2.2.2.2"
    assert ip_cliente({}, None) == "desconocida"


def test_normalizar_ip():
    assert normalizar_ip("2001:db8:1:1:aaaa::1") == "2001:db8:1:1::/64"
    assert normalizar_ip("::ffff:1.2.3.4") == "1.2.3.4"
    assert normalizar_ip("basura") == "desconocida"


def test_hash_cambia_por_dia_y_no_contiene_la_ip():
    a = hash_ip("1.2.3.4", "sal", "2026-10-22")
    assert a != hash_ip("1.2.3.4", "sal", "2026-10-23")
    assert "1.2.3.4" not in a and len(a) == 32
