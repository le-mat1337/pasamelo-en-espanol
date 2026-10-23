"""Errores con código estable y mensaje para la persona que usa la página.

El código viaja al frontend y a la analítica; el mensaje se muestra tal cual.
Nunca se manda al usuario un stack trace ni texto de un servicio externo.
"""

from __future__ import annotations


class PesError(Exception):
    """Error esperado: tiene código, mensaje en español y status HTTP."""

    http = 400

    def __init__(self, codigo: str, mensaje: str, http: int | None = None, devolver_cupo: bool = False):
        super().__init__(codigo)
        self.codigo = codigo
        self.mensaje = mensaje
        if http is not None:
            self.http = http
        # Si el error es culpa nuestra o de la plataforma, el cupo diario se devuelve.
        self.devolver_cupo = devolver_cupo

    def a_dict(self) -> dict[str, str]:
        return {"codigo": self.codigo, "mensaje": self.mensaje}


MENSAJES: dict[str, str] = {
    # Voz de la página: bardeo cariñoso, sin insultos; cada mensaje dice qué pasó y qué hacer.
    "link_invalido": "Eso no es un reel ni un TikTok, capo. Copiá el link desde Compartir.",
    "carrusel": "Eso es un carrusel de fotos. Necesito un video.",
    "no_video": "En ese link no hay ningún video que pueda bajar.",
    "no_disponible": "Ese video es privado, se borró o no se ve desde acá.",
    "bloqueado": "No me dejan bajar ese video ahora. Probá en un rato o con otro.",
    "ig_vacio": "Instagram se puso denso y no me deja bajar ese reel. Probá en un rato o con un TikTok.",
    "muy_largo": "Ese video es muy largo, che. Eso ya es una película.",
    "muy_pesado": "Ese video pesa demasiado. Probá con uno más corto.",
    "sin_voz": "Ahí no habla nadie. ¿Qué querés que traduzca, la música?",
    "no_ingles": "Ese video no está en inglés. Por ahora solo traduzco del inglés.",
    "traduccion": "No entendí nada, soy de Boca. Probá de nuevo en un rato.",
    "ocupado": "Hay cola. Probá en un minuto.",
    "interno": "Se rompió algo de mi lado. Probá de nuevo en un rato.",
    "cortado": "Se cortó a mitad de camino. Probá de nuevo.",
    "captcha": "No pude confirmar que sos una persona. Tocá la casilla y dale de nuevo.",
    "cupo_ip": "Ya usaste los videos de hoy. Mañana seguimos, capo.",
    "en_curso": "Ya estás traduciendo un video. Esperá a que termine.",
    "cupo_global": "Hoy ya se llenó. Volvé mañana, capo.",
    "cupo_hora": "Hay cola. Probá en un rato.",
    "pausado": "Estoy en pausa un rato. Volvé más tarde.",
    "config": "La página no está bien configurada. Ya me enteré.",
    "no_encontrado": "Ese video ya se borró: duran 24 h. Pedí que te lo pasen de nuevo.",
}


def error(codigo: str, http: int = 400, devolver_cupo: bool = False) -> PesError:
    return PesError(codigo, MENSAJES[codigo], http=http, devolver_cupo=devolver_cupo)
