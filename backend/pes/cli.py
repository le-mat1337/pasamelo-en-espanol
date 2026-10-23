"""Pipeline local sin deploy: imprime el WebVTT en español.

    uv run python -m pes.cli "https://www.tiktok.com/@alguien/video/123"
    uv run python -m pes.cli video.mp4 --whisper-json transcripcion.json
    uv run python -m pes.cli "<url>" --solo-bajar

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
            if args.sin_traducir:
                textos = [b.texto for b in bloques]
            else:
                textos = await crear_traductor(cfg, cliente)([b.texto for b in bloques])
        cues = armar_cues([(b.ini, b.fin, t) for b, t in zip(bloques, textos, strict=True)])
        print(a_vtt(cues))
        print(f"# {len(cues)} cues en {time.monotonic() - t0:.1f} s", file=sys.stderr)
    return 0


def main() -> None:
    configuracion.cargar_env_local()
    p = argparse.ArgumentParser(prog="pes.cli", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("entrada", help="link de Instagram/TikTok o un archivo de video local")
    p.add_argument("--whisper-json", help="usar una transcripción de whisper ya hecha (JSON) en vez de Groq")
    p.add_argument("--sin-traducir", action="store_true", help="imprimir los subtítulos en inglés")
    p.add_argument("--solo-bajar", action="store_true", help="solo descargar y extraer audio")
    try:
        sys.exit(asyncio.run(principal(p.parse_args())))
    except PesError as e:
        print(f"error: {e.codigo}: {e.mensaje}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
