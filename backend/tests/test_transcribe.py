import httpx
import pytest

from pes.errores import PesError
from pes.transcribe import normalizar, transcribir_groq

pytestmark = pytest.mark.anyio

GROQ = {
    "language": "English",
    "duration": 5.0,
    "segments": [
        {"start": 0.0, "end": 2.0, "text": " Hello there. ", "no_speech_prob": 0.01, "avg_logprob": -0.2},
        {"start": 2.0, "end": 4.0, "text": " Thank you.", "no_speech_prob": 0.95, "avg_logprob": -1.2},
        {"start": 4.0, "end": 5.0, "text": "  Bye  now ", "no_speech_prob": 0.1, "avg_logprob": -0.3},
    ],
    "words": [
        {"word": "Hello", "start": 0.1, "end": 0.5},
        {"word": "there.", "start": 0.6, "end": 1.2},
        {"word": "Bye", "start": 4.1, "end": 4.4},
        {"word": "now", "start": 4.5, "end": 4.9},
    ],
}

LOCAL = {
    "language": "en",
    "segments": [
        {"start": 0.0, "end": 1.0, "text": "Hi", "words": [{"word": " Hi", "start": 0.1, "end": 0.6}]},
    ],
}


def test_normalizar_groq():
    t = normalizar(GROQ)
    assert t.es_ingles
    assert [s.texto for s in t.segmentos] == ["Hello there.", "Bye now"]  # el "Thank you." alucinado se va
    assert [p.texto for p in t.segmentos[0].palabras] == ["Hello", "there."]
    assert [p.texto for p in t.segmentos[1].palabras] == ["Bye", "now"]
    assert t.cantidad_palabras == 4


def test_normalizar_whisper_local():
    t = normalizar(LOCAL)
    assert t.es_ingles and t.segmentos[0].palabras[0].texto == "Hi"
    assert t.duracion == 1.0


def test_no_ingles():
    assert not normalizar({"language": "spanish", "segments": []}).es_ingles


async def test_transcribir_groq_manda_lo_correcto(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3" + b"0" * 2000)

    def handler(request: httpx.Request) -> httpx.Response:
        cuerpo = request.content
        assert b'name="model"' in cuerpo and b"whisper-large-v3-turbo" in cuerpo
        assert cuerpo.count(b'name="timestamp_granularities[]"') == 2
        assert b"verbose_json" in cuerpo
        return httpx.Response(200, json=GROQ)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cliente:
        t = await transcribir_groq(audio, "k", "whisper-large-v3-turbo", cliente)
    assert t.es_ingles


async def test_transcribir_groq_429(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x" * 2000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as cliente:
        with pytest.raises(PesError) as e:
            await transcribir_groq(audio, "k", "m", cliente)
    assert e.value.codigo == "ocupado" and e.value.devolver_cupo
