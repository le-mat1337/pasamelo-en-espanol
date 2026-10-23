"""Cloudflare R2 por su API compatible con S3, firmando a mano (AWS SigV4 por query string).

Sin boto3: alcanzan hmac y hashlib, y el bundle no suma decenas de MB. El bucket es
privado: cada video se ve con una URL firmada que vence cuando vence el job.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import xml.etree.ElementTree as ET  # noqa: S405 - solo parsea respuestas de la API de R2
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

from .errores import error

log = logging.getLogger("pes")

MAX_EXPIRA_S = 7 * 24 * 3600  # tope de SigV4
REGION = "auto"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
RE_CUENTA = re.compile(r"^[0-9a-f]{32}$")
RE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
VARIABLES = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def _q(texto: str, seguro: str = "-_.~") -> str:
    return quote(texto, safe=seguro)


def firmar_url(
    metodo: str,
    host: str,
    ruta: str,
    *,
    region: str,
    acceso: str,
    secreto: str,
    ahora: datetime,
    expira_s: int,
    params: Mapping[str, str] | None = None,
) -> str:
    """URL prefirmada de S3 (SigV4, solo la cabecera host firmada, payload sin firmar)."""
    fecha = ahora.strftime("%Y%m%d")
    momento = ahora.strftime("%Y%m%dT%H%M%SZ")
    alcance = f"{fecha}/{region}/s3/aws4_request"
    consulta = {
        **(params or {}),
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{acceso}/{alcance}",
        "X-Amz-Date": momento,
        "X-Amz-Expires": str(expira_s),
        "X-Amz-SignedHeaders": "host",
    }
    qs = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(consulta.items()))
    ruta = _q(ruta, "-_.~/")
    pedido = "\n".join([metodo, ruta, qs, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    a_firmar = "\n".join(["AWS4-HMAC-SHA256", momento, alcance, hashlib.sha256(pedido.encode()).hexdigest()])
    clave = f"AWS4{secreto}".encode()
    for parte in (fecha, region, "s3", "aws4_request"):
        clave = hmac.new(clave, parte.encode(), hashlib.sha256).digest()
    firma = hmac.new(clave, a_firmar.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{ruta}?{qs}&X-Amz-Signature={firma}"


@dataclass(frozen=True)
class CredencialesR2:
    cuenta: str
    acceso: str
    secreto: str = field(repr=False)
    bucket: str

    @property
    def host(self) -> str:
        return f"{self.cuenta}.r2.cloudflarestorage.com"

    @classmethod
    def desde_env(cls, env: Mapping[str, str] = os.environ) -> CredencialesR2 | None:
        """None si R2 no está configurado; error de configuración si lo está a medias o mal."""
        valores = {v: env.get(v, "").strip() for v in VARIABLES}
        if not any(valores.values()):
            return None
        faltan = [v for v, x in valores.items() if not x]
        cred = cls(*valores.values())
        if faltan or not RE_CUENTA.match(cred.cuenta) or not RE_BUCKET.match(cred.bucket):
            log.error("R2 mal configurado (faltan: %s)", ", ".join(faltan) or "ninguna, revisar formato")
            raise error("config", http=503)
        return cred

    def url(
        self, metodo: str, clave: str, expira_s: int, ahora: datetime, params: Mapping[str, str] | None = None
    ) -> str:
        ruta = f"/{self.bucket}/{clave}" if clave else f"/{self.bucket}"
        return firmar_url(
            metodo,
            self.host,
            ruta,
            region=REGION,
            acceso=self.acceso,
            secreto=self.secreto,
            ahora=ahora,
            expira_s=max(1, min(MAX_EXPIRA_S, expira_s)),
            params=params,
        )


@dataclass(frozen=True)
class Objeto:
    clave: str
    modificado: float


def parsear_listado(xml: bytes) -> tuple[list[Objeto], str | None]:
    """Respuesta de ListObjectsV2 → (objetos, token para la página siguiente)."""
    raiz = ET.fromstring(xml)  # noqa: S314 - viene de R2, no del usuario
    objetos = []
    for c in raiz.iter(f"{NS}Contents"):
        clave = c.findtext(f"{NS}Key") or ""
        fecha = c.findtext(f"{NS}LastModified") or ""
        try:
            modificado = datetime.fromisoformat(fecha.replace("Z", "+00:00")).astimezone(UTC).timestamp()
        except ValueError:
            continue
        objetos.append(Objeto(clave, modificado))
    truncado = (raiz.findtext(f"{NS}IsTruncated") or "").lower() == "true"
    return objetos, (raiz.findtext(f"{NS}NextContinuationToken") if truncado else None)
