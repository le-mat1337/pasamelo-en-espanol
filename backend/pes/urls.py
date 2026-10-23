"""Validación y canonización de links. Toda entrada del usuario pasa por acá.

Reglas:
- Solo https y solo hosts de una lista blanca exacta. Nunca se pide nada a otro host.
- Sin usuario/contraseña en la URL, sin puertos raros.
- Los links cortos (vm.tiktok.com, instagram.com/share/...) se resuelven a mano,
  sin seguir redirecciones automáticamente, máximo 3 saltos, y cada salto vuelve a
  pasar por la lista blanca.
- A yt-dlp le llega una URL reconstruida por nosotros, nunca el texto original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

from .errores import PesError, error

Plataforma = Literal["instagram", "tiktok"]

MAX_LARGO = 2048
MAX_SALTOS = 3

HOSTS_INSTAGRAM = {"instagram.com", "www.instagram.com", "m.instagram.com"}
HOSTS_TIKTOK = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
HOSTS_TIKTOK_CORTOS = {"vm.tiktok.com", "vt.tiktok.com"}
HOSTS_PERMITIDOS = HOSTS_INSTAGRAM | HOSTS_TIKTOK | HOSTS_TIKTOK_CORTOS

RE_IG_POST = re.compile(r"^/(?:[A-Za-z0-9._]{1,30}/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]{5,64})/?$")
RE_IG_SHARE = re.compile(r"^/share/(?:reel/|p/)?([A-Za-z0-9_-]{5,64})/?$")
RE_TT_VIDEO = re.compile(r"^/@([A-Za-z0-9._-]{1,64})/video/(\d{8,25})/?$")
RE_TT_FOTO = re.compile(r"^/@[A-Za-z0-9._-]{1,64}/photo/\d{8,25}/?$")
RE_TT_MOVIL = re.compile(r"^/v/(\d{8,25})(?:\.html)?/?$")
RE_TT_CORTO_PATH = re.compile(r"^/(?:t/)?([A-Za-z0-9]{5,20})/?$")
RE_PRIMERA_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
RE_SIN_ESQUEMA = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}/", re.IGNORECASE)

UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Mobile Safari/537.36"


@dataclass(frozen=True)
class VideoRef:
    plataforma: Plataforma
    id: str
    url: str  # URL canónica armada por nosotros: es lo único que ve yt-dlp

    @property
    def clave(self) -> str:
        return f"{self.plataforma}:{self.id}"


@dataclass(frozen=True)
class LinkCorto:
    url: str


def extraer_url(texto: str) -> str:
    """La gente pega "Mirá esto https://vm.tiktok.com/xyz/ jaja". Nos quedamos con la primera URL."""
    if not isinstance(texto, str):
        raise error("link_invalido")
    texto = texto.strip()
    if not texto or len(texto) > MAX_LARGO:
        raise error("link_invalido")
    encontrada = RE_PRIMERA_URL.search(texto)
    if encontrada:
        return encontrada.group(0).rstrip(".,;:!?)]}")
    # Sin esquema: "instagram.com/reel/abc". Solo si empieza con un dominio, nada de "javascript:".
    primera = texto.split()[0]
    if RE_SIN_ESQUEMA.match(primera):
        return "https://" + primera
    raise error("link_invalido")


def _host_y_path(url: str) -> tuple[str, str]:
    try:
        partes = urlsplit(url)
        puerto = partes.port  # levanta ValueError con puertos basura ("javascript:alert(1)")
    except ValueError:
        raise error("link_invalido") from None
    if partes.scheme.lower() not in {"http", "https"}:
        raise error("link_invalido")
    # Sin credenciales embebidas ni puertos: "https://instagram.com@evil.com" o ":8080" no pasan.
    if partes.username or partes.password or puerto not in (None, 80, 443):
        raise error("link_invalido")
    host = (partes.hostname or "").lower().rstrip(".")
    if host not in HOSTS_PERMITIDOS:
        raise error("link_invalido")
    path = re.sub(r"/{2,}", "/", partes.path or "/")
    return host, path


def parsear(url: str) -> VideoRef | LinkCorto:
    """Clasifica una URL ya extraída. No hace ningún pedido de red."""
    host, path = _host_y_path(url)

    if host in HOSTS_INSTAGRAM:
        # "share" primero: si no, /share/reel/<x> parece /<usuario>/reel/<x>.
        if RE_IG_SHARE.match(path):
            return LinkCorto(f"https://www.instagram.com{path}")
        if m := RE_IG_POST.match(path):
            codigo = m.group(1)
            return VideoRef("instagram", codigo, f"https://www.instagram.com/reel/{codigo}/")
        raise error("link_invalido")

    if host in HOSTS_TIKTOK_CORTOS:
        if m := RE_TT_CORTO_PATH.match(path):
            return LinkCorto(f"https://{host}/{m.group(1)}/")
        raise error("link_invalido")

    # tiktok.com, www.tiktok.com, m.tiktok.com
    if m := RE_TT_VIDEO.match(path):
        usuario, vid = m.group(1), m.group(2)
        return VideoRef("tiktok", vid, f"https://www.tiktok.com/@{usuario}/video/{vid}")
    if RE_TT_FOTO.match(path):
        raise error("carrusel")
    if m := RE_TT_MOVIL.match(path):
        vid = m.group(1)
        return VideoRef("tiktok", vid, f"https://m.tiktok.com/v/{vid}.html")
    if path.startswith("/t/") and (m := RE_TT_CORTO_PATH.match(path)):
        return LinkCorto(f"https://www.tiktok.com/t/{m.group(1)}/")
    raise error("link_invalido")


async def resolver(texto: str, cliente: httpx.AsyncClient | None = None) -> VideoRef:
    """Texto pegado por el usuario -> VideoRef. Resuelve links cortos con a lo sumo 3 saltos."""
    actual = parsear(extraer_url(texto))
    if isinstance(actual, VideoRef):
        return actual

    propio = cliente is None
    cliente = cliente or httpx.AsyncClient(timeout=8.0, follow_redirects=False, headers={"User-Agent": UA})
    try:
        url = actual.url
        for _ in range(MAX_SALTOS):
            try:
                resp = await cliente.get(url, follow_redirects=False)
            except httpx.HTTPError:
                raise error("no_disponible", http=502) from None
            destino = resp.headers.get("location")
            if resp.status_code not in (301, 302, 303, 307, 308) or not destino:
                break
            destino = urljoin(url, destino)
            try:
                siguiente = parsear(destino)  # vuelve a pasar por la lista blanca
            except PesError:
                host = (urlsplit(destino).hostname or "").lower()
                if host in HOSTS_PERMITIDOS:
                    raise error("no_disponible", http=422) from None  # el link corto no lleva a un video
                raise
            if isinstance(siguiente, VideoRef):
                return siguiente
            url = siguiente.url
        raise error("no_disponible", http=422)
    finally:
        if propio:
            await cliente.aclose()
