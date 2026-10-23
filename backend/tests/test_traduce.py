import dataclasses
import json

import httpx
import pytest

from pes import config as configuracion
from pes.errores import PesError
from pes.traduce import SalidaInvalida, entrada, limpiar, traductor_groq, validar

pytestmark = pytest.mark.anyio

ORIG = ["Hello my friend.", "This is crazy.", "Look at that!"]


def _salida(textos, cancion=False):
    return json.dumps({"items": [{"i": i, "t": t} for i, t in enumerate(textos)], "cancion": cancion})


def test_entrada_es_json_con_indices():
    datos = json.loads(entrada(ORIG))
    assert [x["i"] for x in datos["items"]] == [0, 1, 2]


def test_validar_ok():
    assert validar(_salida(["Hola, amigo.", "Esto es una locura.", "¡Mirá eso!"]), ORIG).textos[2] == "¡Mirá eso!"


def test_validar_limpia_markup_y_control():
    salida = validar(_salida(["<b>Hola</b>‮ amigo", '"Esto es una locura."', "¡Mirá eso!"]), ORIG).textos
    assert salida[0] == "Hola amigo"
    assert salida[1] == "Esto es una locura."


def test_validar_un_bloque_malo_queda_en_ingles():
    crudo = json.dumps({"items": [{"i": 0, "t": "Hola"}, {"i": 1, "t": "x" * 500}, {"i": 2, "t": "Mirá"}]})
    with pytest.raises(SalidaInvalida):  # 1 de 3 = 33 % > 30 %
        validar(crudo, ORIG)
    largos = ORIG + ["a", "b", "c", "d"]
    crudo = _salida(["Hola", "Locura", "Mirá", "a", "b", "c", "x" * 500])
    assert validar(crudo, largos).textos[6] == "d"


def test_validar_indices_faltantes():
    with pytest.raises(SalidaInvalida):
        validar(json.dumps({"items": [{"i": 0, "t": "Hola"}]}), ORIG)


@pytest.mark.parametrize("crudo", ["no es json", "{}", '{"items": "x"}', "[]"])
def test_validar_basura(crudo):
    with pytest.raises(SalidaInvalida):
        validar(crudo, ORIG)


def test_limpiar_comillas():
    assert limpiar("«Che»") == "Che"


def _cfg():
    return dataclasses.replace(configuracion.cargar(), groq_api_key="gsk_test", traductor="groq")


async def test_groq_reintenta_una_vez_y_valida():
    llamadas = []

    def handler(request: httpx.Request) -> httpx.Response:
        cuerpo = json.loads(request.content)
        llamadas.append(cuerpo)
        assert cuerpo["response_format"]["json_schema"]["strict"] is True
        assert "cancion" in cuerpo["response_format"]["json_schema"]["schema"]["required"]
        assert request.headers["authorization"] == "Bearer gsk_test"
        contenido = "basura" if len(llamadas) == 1 else _salida(["Hola, amigo.", "Una locura.", "¡Mirá!"])
        return httpx.Response(200, json={"choices": [{"message": {"content": contenido}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cliente:
        traducir = traductor_groq(_cfg(), cliente)
        assert (await traducir(ORIG)).textos == ["Hola, amigo.", "Una locura.", "¡Mirá!"]
    assert len(llamadas) == 2


@pytest.fixture
def esperas(monkeypatch):
    """Los reintentos esperan: acá se anotan en vez de dormir."""
    anotadas: list[float] = []

    async def dormir(segundos):
        anotadas.append(segundos)

    monkeypatch.setattr("pes.traduce.asyncio.sleep", dormir)
    return anotadas


async def test_groq_falla_tres_veces(esperas):
    llamadas = []

    def handler(request):
        llamadas.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cliente:
        with pytest.raises(PesError) as e:
            await traductor_groq(_cfg(), cliente)(ORIG)
    assert e.value.codigo == "traduccion" and e.value.devolver_cupo
    assert len(llamadas) == 3


async def test_groq_json_validate_failed_se_reintenta(esperas):
    # Groq devuelve este 400 seguido con json_schema estricto: no es culpa del pedido
    llamadas = []

    def handler(request):
        llamadas.append(1)
        if len(llamadas) < 3:
            return httpx.Response(400, json={"error": {"code": "json_validate_failed", "failed_generation": ""}})
        return httpx.Response(200, json={"choices": [{"message": {"content": _salida(["Hola", "Locura", "Mirá"])}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cliente:
        assert (await traductor_groq(_cfg(), cliente)(ORIG)).textos == ["Hola", "Locura", "Mirá"]
    assert len(llamadas) == 3


async def test_groq_429_corto_espera_y_sigue(esperas):
    llamadas = []

    def handler(request):
        llamadas.append(1)
        if len(llamadas) == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, json={"choices": [{"message": {"content": _salida(["Hola", "Locura", "Mirá"])}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cliente:
        assert (await traductor_groq(_cfg(), cliente)(ORIG)).textos == ["Hola", "Locura", "Mirá"]
    assert esperas == [7.0]


@pytest.mark.parametrize("cabeceras", [{}, {"retry-after": "120"}])
async def test_groq_429_largo_o_sin_aviso_es_ocupado(esperas, cabeceras):
    transporte = httpx.MockTransport(lambda r: httpx.Response(429, headers=cabeceras))
    async with httpx.AsyncClient(transport=transporte) as cliente:
        with pytest.raises(PesError) as e:
            await traductor_groq(_cfg(), cliente)(ORIG)
    assert e.value.codigo == "ocupado"


def test_validar_bloques_juntados_o_corridos_se_rechazan():
    # El modelo juntó dos frases: los textos ya no coinciden con sus tiempos
    with pytest.raises(SalidaInvalida):
        validar(_salida(["Hola, amigo. Esto es una locura.", "¡Mirá eso!"]), ORIG)
    with pytest.raises(SalidaInvalida):
        validar(_salida(["Hola", "Locura", "Mirá", "sobra"]), ORIG)


def test_validar_bloque_sin_traducir_cuenta_como_malo():
    originales = ["one two three", "four five six", "seven eight nine", "ten eleven twelve"]
    salida = validar(
        _salida(["uno dos tres", "cuatro cinco seis", "siete ocho nueve", "ten eleven twelve"]), originales
    ).textos
    assert salida[3] == "ten eleven twelve"  # 1 de 4 queda en inglés: se acepta
    with pytest.raises(SalidaInvalida):  # 2 de 4 sin traducir: se reintenta
        validar(_salida(["uno dos tres", "four five six", "siete ocho nueve", "ten eleven twelve"]), originales)
    assert validar(_salida(["OK", "Jett", "Che"]), ["OK", "Jett", "Che"]).textos == ["OK", "Jett", "Che"]  # nombres


def test_validar_lee_cancion():
    assert validar(_salida(["Hola", "Locura", "Mirá"], cancion=True), ORIG).cancion is True
    assert validar(_salida(["Hola", "Locura", "Mirá"]), ORIG).cancion is False
    assert (
        validar(json.dumps({"items": json.loads(_salida(["Hola", "Locura", "Mirá"]))["items"]}), ORIG).cancion is False
    )


def test_validar_tolerantes_no_cuentan_como_malos():
    # El caption (último) ya estaba en castellano y vuelve igual: no es un error
    originales = ["one two three", "four five six", "esto ya está en castellano"]
    salida = validar(_salida(["uno dos tres", "cuatro cinco seis", "esto ya está en castellano"]), originales,
                     tolerantes=frozenset({2}))  # fmt: skip
    assert salida.textos[2] == "esto ya está en castellano"
    # Texto escrito: todos tolerantes, ninguno traducido ni válido, igual no falla
    vistos = ["POV: me and the boys", "this one goes hard", "x"]
    salida = validar(_salida(vistos[:2] + ["x" * 500]), vistos, tolerantes=frozenset(range(3)))
    assert salida.textos == vistos
    # Los que no son tolerantes siguen contando
    with pytest.raises(SalidaInvalida):
        validar(_salida(["one two three", "four five six", "hola"]), originales, tolerantes=frozenset({2}))
