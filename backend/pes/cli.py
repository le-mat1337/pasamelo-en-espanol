"""Pipeline local sin deploy: imprime el WebVTT en español.

    uv run python -m pes.cli "https://www.tiktok.com/@alguien/video/123"
    uv run python -m pes.cli video.mp4 --whisper-json transcripcion.json
    uv run python -m pes.cli "<url>" --solo-bajar
    uv run python -m pes.cli "<url>" --texto       # también el texto escrito sobre el video

Necesita GROQ_API_KEY (salvo con --whisper-json) y, para traducir, la clave del traductor elegido.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

from . import config as configuracion
from .cues import a_vtt, armar_cues, partir_para_traducir
from .descarga import bajar_video, extraer_audio, preparar_video
from .errores import PesError
from .texto import leer as leer_texto
from .traduce import crear_traductor
from .transcribe import normalizar, transcribir_groq
from .urls import UA, resolver


async def principal(args: argparse.Namespace) -> int:
    cfg = configuracion.cargar()
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pes-cli-") as tmp:
        carpeta = Path(tmp)
        entrada = Path(args.entrada)
        if await asyncio.to_thread(entrada.is_file):
            crudo = await asyncio.to_thread(entrada.resolve)
        else:
            ref = await resolver(args.entrada)
            print(f"# {ref.plataforma} {ref.id} -> {ref.url}", file=sys.stderr)
            crudo, info = await asyncio.to_thread(bajar_video, ref, carpeta, cfg.max_mb, cfg.max_duracion_s)
            print(f"# bajado: {crudo.stat().st_size / 1e6:.1f} MB, autor={info.get('uploader')}", file=sys.stderr)
        video, duracion = await asyncio.to_thread(preparar_video, crudo, carpeta, cfg.max_duracion_s)
        audio = await asyncio.to_thread(extraer_audio, video, carpeta, cfg.max_duracion_s)
        print(f"# video {duracion:.1f} s, audio {audio.stat().st_size / 1e3:.0f} KB ({time.monotonic() - t0:.1f} s)",
              file=sys.stderr)  # fmt: skip
        if args.solo_bajar:
            return 0

        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30.0) as cliente:
            if args.whisper_json:
                crudo_json = await asyncio.to_thread(Path(args.whisper_json).read_text)
                transcripcion = normalizar(json.loads(crudo_json))
            else:
                transcripcion = await transcribir_groq(audio, cfg.groq_api_key, cfg.modelo_groq_whisper, cliente)
            print(f"# idioma={transcripcion.idioma} palabras={transcripcion.cantidad_palabras}", file=sys.stderr)
            bloques = partir_para_traducir(transcripcion.segmentos)
            traducir = crear_traductor(cfg, cliente)
            if args.sin_traducir or not bloques:
                textos = [b.texto for b in bloques]
            else:
                tr = await traducir([b.texto for b in bloques])
                textos = tr.textos
                print(f"# cancion={tr.cancion}", file=sys.stderr)
            if args.texto:
                await _texto_escrito(video, duracion, cfg, cliente, traducir, args.sin_traducir)
        cues = armar_cues([(b.ini, b.fin, t) for b, t in zip(bloques, textos, strict=True)])
        print(a_vtt(cues))
        print(f"# {len(cues)} cues en {time.monotonic() - t0:.1f} s", file=sys.stderr)
    return 0


async def _texto_escrito(video, duracion, cfg, cliente, traducir, sin_traducir: bool) -> None:
    """Lee el texto escrito sobre el video y lo imprime (con la traducción del que está en inglés)."""
    t0 = time.monotonic()
    vistos = await leer_texto(video, duracion, cfg=cfg, cliente=cliente)
    ingles = [v.texto for v in vistos if v.en]
    traducidos = dict(zip(ingles, ingles, strict=True))
    if ingles and not sin_traducir:
        tr = await traducir(ingles, tolerantes=frozenset(range(len(ingles))))
        traducidos = dict(zip(ingles, tr.textos, strict=True))
    print(f"# texto escrito: {len(vistos)} ({time.monotonic() - t0:.1f} s)", file=sys.stderr)
    for v in vistos:
        print(f"# {v.ini:5.1f}-{v.fin:5.1f} {v.pos} {'en' if v.en else '--'} {v.texto}", file=sys.stderr)
        if v.en:
            print(f"#             -> {traducidos[v.texto]}", file=sys.stderr)


def main() -> None:
    configuracion.cargar_env_local()
    p = argparse.ArgumentParser(prog="pes.cli", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("entrada", help="link de Instagram/TikTok o un archivo de video local")
    p.add_argument("--whisper-json", help="usar una transcripción de whisper ya hecha (JSON) en vez de Groq")
    p.add_argument("--sin-traducir", action="store_true", help="imprimir los subtítulos en inglés")
    p.add_argument("--solo-bajar", action="store_true", help="solo descargar y extraer audio")
    p.add_argument("--texto", action="store_true", help="leer también el texto escrito sobre el video (Groq visión)")
    try:
        sys.exit(asyncio.run(principal(p.parse_args())))
    except PesError as e:
        print(f"error: {e.codigo}: {e.mensaje}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
