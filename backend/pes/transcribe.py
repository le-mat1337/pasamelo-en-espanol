"""Transcripción con Groq Whisper (whisper-large-v3-turbo, tier gratis).

Groq devuelve `verbose_json` con segmentos y, si se piden, palabras con timestamps.
`normalizar` también acepta el JSON del CLI `whisper` local (palabras dentro de cada
segmento), que se usa para probar sin claves.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .errores import error

GROQ_TRANSCRIPCION = "https://api.groq.com/openai/v1/audio/transcriptions"
log = logging.getLogger("pes")
IDIOMAS_INGLES = {"en", "eng", "english", "inglés", "ingles"}


@dataclass(frozen=True)
class Palabra:
    texto: str
    ini: float
    fin: float


@dataclass
class Segmento:
    ini: float
    fin: float
    texto: str
    palabras: list[Palabra] = field(default_factory=list)


@dataclass
class Transcripcion:
    idioma: str
    duracion: float
    segmentos: list[Segmento]

    @property
    def es_ingles(self) -> bool:
        return self.idioma.strip().lower() in IDIOMAS_INGLES

    @property
    def cantidad_palabras(self) -> int:
        return sum(len(s.texto.split()) for s in self.segmentos)


def _palabras(crudas: list[dict]) -> list[Palabra]:
    salida = []
    for w in crudas or []:
        texto = str(w.get("word") or "").strip()
        if not texto:
            continue
        try:
            salida.append(Palabra(texto, float(w["start"]), float(w["end"])))
        except (KeyError, TypeError, ValueError):
            continue
    return salida


def normalizar(j: dict) -> Transcripcion:
    segmentos: list[Segmento] = []
    for s in j.get("segments") or []:
        texto = " ".join(str(s.get("text") or "").split())
        if not texto:
            continue
        try:
            ini, fin = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        # Whisper "alucina" frases sobre música: si dice que no hay voz y además duda, se descarta.
        sin_voz = float(s.get("no_speech_prob") or 0.0)
        confianza = float(s.get("avg_logprob") or 0.0)
        if sin_voz > 0.8 and confianza < -0.7:
            continue
        segmentos.append(Segmento(ini, max(fin, ini + 0.2), texto, _palabras(s.get("words") or [])))

    # Groq devuelve las palabras al nivel raíz: se reparten por el punto medio de cada una.
    sueltas = _palabras(j.get("words") or [])
    if sueltas and not any(s.palabras for s in segmentos):
        for p in sueltas:
            medio = (p.ini + p.fin) / 2
            for s in segmentos:
                if s.ini - 0.05 <= medio <= s.fin + 0.05:
                    s.palabras.append(p)
                    break

    duracion = float(j.get("duration") or (segmentos[-1].fin if segmentos else 0.0))
    return Transcripcion(idioma=str(j.get("language") or ""), duracion=duracion, segmentos=segmentos)


ESPERA_MAX_S = 20.0  # si Groq pide esperar más que esto, se avisa "ocupado" en vez de colgar al usuario


def segundos_de_espera(resp: httpx.Response) -> float | None:
    """Cuánto pide esperar Groq en un 429 (cabecera retry-after), o None si no lo dice."""
    try:
        return max(0.0, float(resp.headers.get("retry-after", "")))
    except ValueError:
        return None


async def transcribir_groq(audio: Path, api_key: str, modelo: str, cliente: httpx.AsyncClient) -> Transcripcion:
    datos = {
        "model": modelo,
        "response_format": "verbose_json",
        "temperature": "0",
        "timestamp_granularities[]": ["word", "segment"],
    }
    archivos = {"file": ("audio.mp3", await asyncio.to_thread(audio.read_bytes), "audio/mpeg")}
    for intento in range(2):
        try:
            resp = await cliente.post(
                GROQ_TRANSCRIPCION,
                data=datos,
                files=archivos,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=90.0,
            )
        except httpx.HTTPError as e:
            log.warning("groq whisper: %s", type(e).__name__)
            raise error("ocupado", http=503, devolver_cupo=True) from None
        if resp.status_code != 429:
            break
        espera = segundos_de_espera(resp)
        log.warning("groq whisper 429, pide esperar %s s", espera)
        if intento or espera is None or espera > ESPERA_MAX_S:
            raise error("ocupado", http=503, devolver_cupo=True)
        await asyncio.sleep(espera)
    if resp.status_code != 200:
        log.warning("groq whisper %s", resp.status_code)
        raise error("interno", http=502, devolver_cupo=True)
    return normalizar(resp.json())
