import pytest

from pes.descarga import MAX_CAPTION, clasificar_error, limpiar_caption


@pytest.mark.parametrize(
    "stderr,plataforma,codigo,devuelve",
    [
        (
            "ERROR: [Instagram] x: Requested content is not available, rate-limit reached or login required",
            "instagram",
            "bloqueado",
            True,
        ),
        ("ERROR: x does not pass filter (!duration | duration <= 180), skipping", "tiktok", "muy_largo", False),
        ("File is larger than max-filesize (90.0MiB > 60.0MiB). Aborting.", "tiktok", "muy_pesado", False),
        ("ERROR: [TikTok] 123: This video is private", "tiktok", "no_disponible", False),
        ("ERROR: [Instagram] x: There is no video in this post", "instagram", "no_video", False),
        ("ERROR: [Instagram] x: HTTP Error 403: Forbidden", "instagram", "bloqueado", True),
        ("ERROR: [TikTok] 1: Your IP address is blocked from accessing this post", "tiktok", "bloqueado", True),
        ("ERROR: [Instagram] x: Instagram sent an empty media response. Check if", "instagram", "ig_vacio", True),
        ("[download] File is larger than max-filesize (9 > 6). Aborting.", "tiktok", "muy_pesado", False),
        ("[info] 1: does not pass filter (duration <=? 20), skipping ..", "tiktok", "muy_largo", False),
        ("algo raro", "tiktok", "interno", True),
    ],
)
def test_clasificar(stderr, plataforma, codigo, devuelve):
    e = clasificar_error(stderr, plataforma)
    assert e.codigo == codigo
    assert e.devolver_cupo is devuelve
    assert stderr not in e.mensaje  # nunca se filtra la salida de yt-dlp al usuario
    if codigo == "bloqueado":
        assert ("Instagram" if plataforma == "instagram" else "TikTok") in e.mensaje


@pytest.mark.parametrize(
    "crudo,limpio",
    [
        ("Wait for it 😂 #fyp #funny #dog", "Wait for it 😂"),
        ("my dog\n\nhates   mondays #monday", "my dog hates mondays"),
        ("#fyp #viral #parati", None),
        ("two words", None),
        (None, None),
        (123, None),
        ("<b>bold</b> text here‮", "bold text here"),
    ],
)
def test_limpiar_caption(crudo, limpio):
    assert limpiar_caption(crudo) == limpio


def test_limpiar_caption_largo_se_corta_en_una_palabra():
    texto = limpiar_caption("word " * 200)
    assert texto.endswith("word…") and len(texto) <= MAX_CAPTION + 1
