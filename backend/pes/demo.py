"""Modo demo (PES_DEMO=1, solo en desarrollo): baja el video de verdad pero inventa los subtítulos.

Sirve para probar la interfaz sin claves de Groq. Nunca se activa en Vercel.
"""

from __future__ import annotations

from pathlib import Path

from .transcribe import Segmento, Transcripcion


async def transcribir(audio: Path, duracion: float = 60.0) -> Transcripcion:
    segmentos = []
    t, n = 0.5, 1
    while t < duracion:
        segmentos.append(Segmento(t, t + 2.6, f"This is demo subtitle number {n}, nothing was transcribed."))
        t += 3.0
        n += 1
    return Transcripcion("english", duracion, segmentos)


async def traducir(textos: list[str]) -> list[str]:
    return [f"Subtítulo de prueba {i + 1}: acá iría la traducción." for i in range(len(textos))]
