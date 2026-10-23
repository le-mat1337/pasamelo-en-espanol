"""Modo demo (PES_DEMO=1, solo en desarrollo): baja el video de verdad pero inventa los subtítulos.

Sirve para probar la interfaz sin claves de Groq. Nunca se activa en Vercel. Dice que todo video
es una canción, así se ven las dos pistas (letra y texto escrito) y el caption.
"""

from __future__ import annotations

from pathlib import Path

from .texto import TextoVisto
from .traduce import NINGUNO, Traduccion
from .transcribe import Segmento, Transcripcion


async def transcribir(audio: Path, duracion: float = 60.0) -> Transcripcion:
    segmentos = []
    t, n = 0.5, 1
    while t < duracion:
        segmentos.append(Segmento(t, t + 2.6, f"This is demo subtitle number {n}, nothing was transcribed."))
        t += 3.0
        n += 1
    return Transcripcion("english", duracion, segmentos)


async def traducir(textos: list[str], tolerantes: frozenset[int] = NINGUNO) -> Traduccion:
    return Traduccion(
        [
            f"Texto de prueba {i + 1}: acá iría lo escrito."
            if i in tolerantes
            else f"Subtítulo de prueba {i + 1}: acá iría la traducción."
            for i in range(len(textos))
        ],
        cancion=True,
    )


async def leer_texto(video: Path, duracion: float) -> list[TextoVisto]:
    return [
        TextoVisto("POV: demo title on top", 0.0, min(duracion, 8.0), True, "top"),
        TextoVisto("demo text in the middle", 8.0, min(duracion, 14.0), True, "mid"),
        TextoVisto("texto que ya estaba en castellano", 14.0, min(duracion, 20.0), False, "bot"),
        TextoVisto("demo text at the bottom", 20.0, min(duracion, 28.0), True, "bot"),
    ]
