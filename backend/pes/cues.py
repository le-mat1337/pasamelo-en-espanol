"""Subtítulos: armar frases completas antes de traducir y cues legibles después.

Whisper corta sus segmentos donde quiere, a veces en medio de una oración ("…what he" |
"can become."). Traducir pedazos así sale mal: el modelo junta o corre texto entre
bloques. Por eso se traduce por frase (todas las palabras, cortadas en fin de oración o
pausa larga) y la traducción se reparte después en cues cortos.

Un cue es lo que se ve en pantalla de una vez: a lo sumo dos renglones cortos.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .transcribe import Palabra, Segmento

MAX_CHARS_ORIGEN = 200  # inglés por frase a traducir: se corta antes solo si no hubo fin de oración
MAX_DUR_ORIGEN = 12.0
PAUSA_CORTE = 1.0  # un silencio así entre palabras también cierra la frase
MAX_CHARS_CUE = 64  # español en pantalla: ~2 renglones de 32
MIN_DUR_CUE = 0.9
FIN_ORACION = re.compile(r"[.!?…][\"')\]]*$")
PAUSA_TEXTO = re.compile(r"[,;:—–-]$")


@dataclass
class Cue:
    ini: float
    fin: float
    texto: str
    pos: str | None = None  # solo el texto escrito: top, mid o bot (dónde estaba el original)

    def a_dict(self) -> dict:
        d = {"s": round(self.ini, 2), "e": round(self.fin, 2), "t": self.texto}
        if self.pos:
            d["p"] = self.pos
        return d


def _unir(palabras: list[Palabra]) -> str:
    texto = " ".join(p.texto.strip() for p in palabras if p.texto.strip())
    return re.sub(r"\s+([,.!?;:…])", r"\1", texto).strip()


def _palabras(segmentos: list[Segmento]) -> list[Palabra]:
    """Todas las palabras en orden. Un segmento sin tiempos por palabra cuenta como una sola."""
    salida: list[Palabra] = []
    for seg in segmentos:
        if seg.palabras:
            salida.extend(seg.palabras)
        elif seg.texto.strip():
            salida.append(Palabra(seg.texto.strip(), seg.ini, seg.fin))
    return salida


def partir_para_traducir(
    segmentos: list[Segmento], max_chars: int = MAX_CHARS_ORIGEN, max_dur: float = MAX_DUR_ORIGEN
) -> list[Segmento]:
    """Agrupa las palabras en frases: fin de oración, pausa larga o, si se pasa del máximo, la última coma."""
    frases: list[Segmento] = []
    actual: list[Palabra] = []

    def cerrar(hasta: int) -> None:
        nonlocal actual
        bloque, actual = actual[:hasta], actual[hasta:]
        if bloque:
            frases.append(Segmento(bloque[0].ini, bloque[-1].fin, _unir(bloque), bloque))

    for p in _palabras(segmentos):
        if actual and p.ini - actual[-1].fin >= PAUSA_CORTE:
            cerrar(len(actual))
        actual.append(p)
        if FIN_ORACION.search(p.texto.strip()):
            cerrar(len(actual))
            continue
        largo, dur = len(_unir(actual)), actual[-1].fin - actual[0].ini
        if len(actual) > 1 and (largo > max_chars or dur > max_dur):
            corte = len(actual) - 1  # sin coma razonable, se corta antes de esta palabra
            for i in range(len(actual) - 1, 0, -1):
                if PAUSA_TEXTO.search(actual[i - 1].texto.strip()) and len(_unir(actual[:i])) >= max_chars * 0.4:
                    corte = i
                    break
            cerrar(corte)
    cerrar(len(actual))
    return frases


def _partir_texto(texto: str, partes: int) -> list[str]:
    palabras = texto.split()
    if partes <= 1 or len(palabras) < 2:
        return [texto]
    objetivo = len(texto) / partes
    trozos: list[str] = []
    actual: list[str] = []
    for i, w in enumerate(palabras):
        actual.append(w)
        restantes_trozos = partes - len(trozos) - 1
        restantes_palabras = len(palabras) - i - 1
        if restantes_trozos and len(" ".join(actual)) >= objetivo and restantes_palabras >= restantes_trozos:
            trozos.append(" ".join(actual))
            actual = []
    if actual:
        trozos.append(" ".join(actual))
    return trozos


def armar_cues(bloques: list[tuple[float, float, str]], max_chars: int = MAX_CHARS_CUE) -> list[Cue]:
    """(ini, fin, texto en español) -> cues cortos, con tiempo repartido por cantidad de letras."""
    cues: list[Cue] = []
    for ini, fin, texto in bloques:
        texto = " ".join(texto.split())
        if not texto:
            continue
        partes = max(1, math.ceil(len(texto) / max_chars))
        trozos = _partir_texto(texto, partes)
        total = sum(len(t) for t in trozos) or 1
        t0 = ini
        for trozo in trozos:
            t1 = t0 + (fin - ini) * len(trozo) / total
            cues.append(Cue(t0, t1, trozo))
            t0 = t1
    return ajustar(cues)


def ajustar(cues: list[Cue], min_dur: float = MIN_DUR_CUE, hueco_max: float = 0.4) -> list[Cue]:
    """Ordena, evita solapamientos, alarga los cues muy cortos y tapa huecos chicos (menos parpadeo)."""
    cues = sorted((c for c in cues if c.texto), key=lambda c: c.ini)
    for i, c in enumerate(cues):
        siguiente = cues[i + 1].ini if i + 1 < len(cues) else None
        if c.fin - c.ini < min_dur:
            c.fin = c.ini + min_dur
        if siguiente is not None:
            if siguiente - c.fin < hueco_max:
                c.fin = siguiente
            c.fin = min(c.fin, siguiente)
        c.fin = max(c.fin, c.ini + 0.3)
    return cues


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, resto = divmod(t, 3600)
    m, s = divmod(resto, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def a_vtt(cues: list[Cue]) -> str:
    lineas = ["WEBVTT", ""]
    for c in cues:
        lineas += [f"{_ts(c.ini)} --> {_ts(c.fin)}", c.texto.replace("-->", "→"), ""]
    return "\n".join(lineas)
