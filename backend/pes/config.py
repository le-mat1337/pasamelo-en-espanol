"""Configuración desde variables de entorno. Ver `.env.example` en la raíz."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def cargar_env_local(ruta: Path | None = None) -> None:
    """Solo desarrollo: lee backend/.env sin pisar variables ya definidas. En Vercel no hace nada."""
    if os.environ.get("VERCEL"):
        return
    ruta = ruta or Path(__file__).resolve().parent.parent / ".env"
    if not ruta.is_file():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        clave, valor = clave.strip(), valor.strip().strip("\"'")
        if clave and valor and clave not in os.environ:
            os.environ[clave] = valor


def _int(nombre: str, defecto: int) -> int:
    valor = os.environ.get(nombre, "").strip()
    try:
        return int(valor) if valor else defecto
    except ValueError:
        return defecto


def _bool(nombre: str) -> bool:
    return os.environ.get(nombre, "").strip().lower() in {"1", "true", "si", "sí", "yes"}


@dataclass(frozen=True)
class Config:
    entorno: str  # production | preview | development
    groq_api_key: str
    traductor: str  # groq | anthropic
    modelo_groq_whisper: str
    modelo_groq_llm: str
    modelo_anthropic: str
    turnstile_secret: str
    cron_secret: str
    sal: str
    max_por_ip_dia: int
    max_jobs_dia: int
    max_jobs_hora: int
    max_duracion_s: int
    max_mb: int
    ttl_horas: int
    vida_max_horas: int
    pausado: bool
    limite_job_s: int = 270  # por debajo de los 300 s por defecto de una función de Vercel
    demo: bool = False  # solo desarrollo: subtítulos de prueba, sin Groq
    hosts_turnstile: tuple[str, ...] = field(default_factory=tuple)

    @property
    def produccion(self) -> bool:
        return self.entorno in {"production", "preview"}

    @property
    def ttl_s(self) -> int:
        return self.ttl_horas * 3600

    def faltantes(self) -> list[str]:
        """Variables obligatorias que faltan. En producción, si falta alguna, no se procesa nada."""
        faltan = []
        if self.demo:
            return faltan
        if not self.groq_api_key:
            faltan.append("GROQ_API_KEY")  # Whisper siempre corre en Groq
        if self.traductor == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
            faltan.append("ANTHROPIC_API_KEY")
        if self.produccion:
            for nombre, valor in (
                ("TURNSTILE_SECRET_KEY", self.turnstile_secret),
                ("CRON_SECRET", self.cron_secret),
                ("PES_SAL", self.sal),
            ):
                if not valor:
                    faltan.append(nombre)
        return faltan


def cargar() -> Config:
    hosts = os.environ.get("PES_HOSTS", "").strip()
    return Config(
        entorno=os.environ.get("VERCEL_ENV", "development"),
        groq_api_key=os.environ.get("GROQ_API_KEY", "").strip(),
        traductor=os.environ.get("TRADUCTOR", "groq").strip().lower() or "groq",
        modelo_groq_whisper=os.environ.get("PES_MODELO_WHISPER", "whisper-large-v3-turbo"),
        modelo_groq_llm=os.environ.get("PES_MODELO_GROQ", "openai/gpt-oss-120b"),
        modelo_anthropic=os.environ.get("PES_MODELO_ANTHROPIC", "claude-haiku-5-5"),
        turnstile_secret=os.environ.get("TURNSTILE_SECRET_KEY", "").strip(),
        cron_secret=os.environ.get("CRON_SECRET", "").strip(),
        sal=os.environ.get("PES_SAL", "").strip()
        or ("solo-para-desarrollo-local" if os.environ.get("VERCEL_ENV", "development") == "development" else ""),
        max_por_ip_dia=_int("PES_MAX_POR_IP_DIA", 3),
        max_jobs_dia=_int("PES_MAX_JOBS_DIA", 80),
        max_jobs_hora=_int("PES_MAX_JOBS_HORA", 15),
        max_duracion_s=_int("PES_MAX_DURACION_S", 180),
        max_mb=_int("PES_MAX_MB", 60),
        ttl_horas=_int("PES_TTL_HORAS", 24),
        vida_max_horas=_int("PES_VIDA_MAX_HORAS", 72),
        pausado=_bool("PES_PAUSADO"),
        demo=_bool("PES_DEMO") and os.environ.get("VERCEL_ENV", "development") == "development",
        hosts_turnstile=tuple(h.strip().lower() for h in hosts.split(",") if h.strip()),
    )
