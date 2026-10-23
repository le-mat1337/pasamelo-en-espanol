"""Descarga del video con yt-dlp y extracción del audio con ffmpeg.

Seguridad:
- yt-dlp y ffmpeg corren como subprocesos con lista de argumentos (sin shell), con timeout,
  dentro de una carpeta temporal propia del job y con un entorno mínimo: no heredan
  GROQ_API_KEY ni ningún otro secreto.
- La URL que recibe yt-dlp es la canónica de `urls.py`, después de `--`.
- Nunca se usan cookies ni cuentas: solo contenido público.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errores import PesError, error
from .traduce import limpiar
from .urls import VideoRef

# H.264 primero (se ve en cualquier celular), sin la versión con marca de agua de TikTok.
# En Instagram los formatos numéricos son los progresivos H.264 con audio.
FORMATO = (
    "b[vcodec^=h264][format_id!=download]"
    "/b[vcodec^=avc1][format_id!=download]"
    "/b[format_id~='^[0-9]+$']"
    "/bv*[vcodec^=avc1]+ba"
    "/b[format_id!=download]"
)
RE_DURACION = re.compile(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


MAX_CAPTION = 300
RE_HASHTAG = re.compile(r"(?<![\w#])#\w+")
RE_PALABRA = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True)
class Descarga:
    video: Path
    audio: Path
    duracion: float
    autor: str | None
    bytes: int
    caption: str | None = None  # lo que el autor escribió en el posteo, ya limpio
    cancion: bool = False  # TikTok dice que suena una canción con nombre (no "original sound")


def limpiar_caption(texto: object) -> str | None:
    """El texto del posteo sin hashtags, en una línea y recortado. None si no queda una frase."""
    if not isinstance(texto, str):
        return None
    texto = limpiar(RE_HASHTAG.sub(" ", texto))
    if len(texto) > MAX_CAPTION:
        texto = texto[:MAX_CAPTION].rsplit(" ", 1)[0].rstrip(" ,;:-—") + "…"
    return texto if len(RE_PALABRA.findall(texto)) >= 3 else None


_FFMPEG: str | None = None


def ffmpeg() -> str:
    """Binario estático de imageio-ffmpeg. Si el bundle perdió el permiso de ejecución
    (el código de la función es de solo lectura), se copia una vez a /tmp."""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    import imageio_ffmpeg  # import tardío: el binario pesa ~80 MB y solo se necesita acá

    ruta = imageio_ffmpeg.get_ffmpeg_exe()
    if not os.access(ruta, os.X_OK):
        copia = Path(tempfile.gettempdir()) / "pes-ffmpeg"
        if not os.access(copia, os.X_OK):
            shutil.copyfile(ruta, copia)
            copia.chmod(0o755)
        ruta = str(copia)
    _FFMPEG = ruta
    return ruta


def _entorno(carpeta: Path) -> dict[str, str]:
    """Entorno mínimo para los subprocesos: sin secretos."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(carpeta), "TMPDIR": str(carpeta)}
    for k in ("LANG", "LC_ALL", "VIRTUAL_ENV", "SSL_CERT_FILE"):
        if k in os.environ:
            env[k] = os.environ[k]
    # El subproceso de yt-dlp tiene que ver las mismas dependencias que este proceso,
    # aunque el runtime las haya agregado a sys.path en tiempo de ejecución.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


def _correr(args: list[str], carpeta: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - lista de argumentos fija, sin shell
        args,
        cwd=carpeta,
        env=_entorno(carpeta),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _bloqueado(plataforma: str) -> PesError:
    nombre = "Instagram" if plataforma == "instagram" else "TikTok"
    return PesError(
        "bloqueado",
        f"{nombre} se puso denso y no me deja bajar ese video ahora. Probá en un rato o con otro.",
        http=503,
        devolver_cupo=True,
    )


BLOQUEOS = (
    "login required", "rate-limit", "rate limit", "http error 429", "http error 401",
    "checkpoint", "ip address is blocked",
)  # fmt: skip
NO_DISPONIBLE = ("private", "unavailable", "not available", "http error 404", "removed", "deleted")
SIN_VIDEO = ("no video", "unsupported url", "no formats", "requested format is not available")


def clasificar_error(salida: str, plataforma: str) -> PesError:
    """Traduce la salida de yt-dlp a un error para el usuario. Nunca se muestra esa salida."""
    s = salida.lower()
    if "does not pass filter" in s:
        return error("muy_largo", http=422)
    if "larger than max-filesize" in s or "file is larger than" in s:
        return error("muy_pesado", http=422)
    if "empty media response" in s:  # Instagram: post privado o muro de login; no se distingue
        return error("ig_vacio", http=422, devolver_cupo=True)
    if any(k in s for k in BLOQUEOS):
        return _bloqueado(plataforma)
    if any(k in s for k in NO_DISPONIBLE):
        return error("no_disponible", http=422)
    if any(k in s for k in SIN_VIDEO):
        return error("no_video", http=422)
    if "http error 403" in s:
        return _bloqueado(plataforma)
    return error("interno", http=502, devolver_cupo=True)


def bajar_video(ref: VideoRef, carpeta: Path, max_mb: int, max_dur: int, timeout: int = 150) -> tuple[Path, dict]:
    args = [
        sys.executable, "-m", "yt_dlp",
        "--no-config", "--no-playlist", "--no-cache-dir", "--no-part", "--no-mtime", "--no-progress",
        "--no-warnings", "--no-quiet",  # -O implica --quiet y esconde los avisos de filtro/tamaño
        "--socket-timeout", "15", "--retries", "2", "--fragment-retries", "2",
        "--max-filesize", f"{max_mb}M",
        "--match-filter", f"duration <=? {max_dur}",
        "--ffmpeg-location", ffmpeg(),
        "-f", FORMATO, "-S", "res:720", "--merge-output-format", "mp4",
        "-o", str(carpeta / "video.%(ext)s"),
        "--no-simulate", "-O", "after_move:%(.{id,duration,uploader,channel,filepath,description,track})j",
        "--", ref.url,
    ]  # fmt: skip
    try:
        proc = _correr(args, carpeta, timeout)
    except subprocess.TimeoutExpired:
        raise error("bloqueado" if ref.plataforma == "instagram" else "interno", http=504, devolver_cupo=True) from None

    info: dict = {}
    for linea in reversed(proc.stdout.strip().splitlines()):
        try:
            info = json.loads(linea)
            break
        except json.JSONDecodeError:
            continue
    ruta = Path(info.get("filepath") or "")
    if not info or not ruta.name or not ruta.is_file():
        raise clasificar_error(f"{proc.stdout}\n{proc.stderr}", ref.plataforma)
    ruta = ruta.resolve()
    if carpeta.resolve() not in ruta.parents:  # el archivo tiene que estar en nuestra carpeta
        raise error("interno", http=500, devolver_cupo=True)
    if ruta.stat().st_size > max_mb * 1024 * 1024:
        raise error("muy_pesado", http=422)
    return ruta, info


def _duracion(stderr: str) -> float | None:
    if m := RE_DURACION.search(stderr):
        h, mi, se = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(se)
    return None


def preparar_video(video: Path, carpeta: Path, max_dur: int, timeout: int = 150) -> tuple[Path, float]:
    """Se asegura de que el video sea H.264/AAC en MP4 (lo reproduce cualquier celular).
    Si yt-dlp tuvo que caer en otro códec, lo recodifica. Devuelve (video, duración)."""
    sonda = _correr([ffmpeg(), "-hide_banner", "-nostdin", "-i", str(video)], carpeta, 30)
    duracion = _duracion(sonda.stderr)
    if duracion is None:
        raise error("no_video", http=422)
    if duracion > max_dur + 2:
        raise error("muy_largo", http=422)
    if "Video: h264" in sonda.stderr and video.suffix == ".mp4":
        return video, duracion
    salida = carpeta / "video-h264.mp4"
    args = [
        ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(video),
        "-t", str(max_dur + 1), "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(salida),
    ]  # fmt: skip
    try:
        proc = _correr(args, carpeta, timeout)
    except subprocess.TimeoutExpired:
        raise error("interno", http=504, devolver_cupo=True) from None
    if proc.returncode != 0 or not salida.is_file():
        raise error("interno", http=502, devolver_cupo=True)
    return salida, duracion


def extraer_audio(video: Path, carpeta: Path, max_dur: int, timeout: int = 60) -> Path:
    """Audio mono 16 kHz en MP3 de 48 kbps: 3 minutos son ~1 MB, lejos del límite de 25 MB de Groq."""
    audio = carpeta / "audio.mp3"
    args = [
        ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(video),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-t", str(max_dur + 1),
        "-c:a", "libmp3lame", "-b:a", "48k", str(audio),
    ]  # fmt: skip
    try:
        proc = _correr(args, carpeta, timeout)
    except subprocess.TimeoutExpired:
        raise error("interno", http=504, devolver_cupo=True) from None
    if proc.returncode != 0 or not audio.is_file() or audio.stat().st_size < 1000:
        raise error("sin_voz", http=422)  # el video no tiene pista de audio
    return audio


def descargar(ref: VideoRef, carpeta: Path, max_mb: int, max_dur: int) -> Descarga:
    """Bloqueante: se llama con asyncio.to_thread."""
    crudo, info = bajar_video(ref, carpeta, max_mb, max_dur)
    video, duracion = preparar_video(crudo, carpeta, max_dur)
    audio = extraer_audio(video, carpeta, max_dur)
    autor = info.get("uploader") or info.get("channel")
    return Descarga(
        video=video,
        audio=audio,
        duracion=duracion,
        autor=str(autor)[:80] if autor else None,
        bytes=video.stat().st_size,
        caption=limpiar_caption(info.get("description")),
        cancion=bool(info.get("track")),
    )
