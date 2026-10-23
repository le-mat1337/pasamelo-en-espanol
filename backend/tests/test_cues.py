from pes.cues import MAX_CHARS_CUE, Cue, a_vtt, ajustar, armar_cues, partir_para_traducir
from pes.transcribe import Palabra, Segmento


def _seg(texto: str, ini: float, paso: float = 0.4) -> Segmento:
    palabras = []
    t = ini
    for w in texto.split():
        palabras.append(Palabra(w, t, t + paso * 0.9))
        t += paso
    return Segmento(ini, t, texto, palabras)


def _textos(segmentos):
    return [x.texto for x in partir_para_traducir(segmentos)]


def test_segmento_corto_queda_igual():
    s = _seg("Hello there, my friend.", 1.0)
    assert _textos([s]) == ["Hello there, my friend."]


def test_frase_partida_por_whisper_se_une():
    # Whisper cortó en medio de la oración: se traduce la oración entera
    a = _seg("there is no peace for the man who has seen what he", 0.0)
    b = _seg("can become. Once a man steps into the light,", a.fin + 0.1)
    frases = partir_para_traducir([a, b])
    assert [f.texto for f in frases] == [
        "there is no peace for the man who has seen what he can become.",
        "Once a man steps into the light,",
    ]
    assert frases[0].ini == 0.0 and frases[0].fin < frases[1].ini


def test_varias_oraciones_en_un_segmento_se_separan():
    assert _textos([_seg("Look at that! Is it real? Yes.", 0.0)]) == ["Look at that!", "Is it real?", "Yes."]


def test_pausa_larga_cierra_la_frase():
    a = _seg("and then he said", 0.0)
    b = _seg("nothing at all", a.fin + 1.5)
    assert _textos([a, b]) == ["and then he said", "nothing at all"]


def test_frase_larga_se_corta_en_la_coma():
    texto = (
        "So I was walking down the street yesterday with my best friend from college and his dog, "
        "and this guy comes up to me and says that he wants to sell me a car that does not even have "
        "wheels on it or a steering wheel or anything at all really"
    )
    partes = partir_para_traducir([_seg(texto, 0.0, paso=0.2)])
    assert len(partes) >= 2
    assert all(len(p.texto) <= 200 for p in partes)
    assert partes[0].texto.endswith(",")  # cortó en la coma
    assert " ".join(p.texto for p in partes) == texto
    for a, b in zip(partes, partes[1:], strict=False):
        assert a.fin <= b.ini + 1e-6


def test_frase_larga_por_duracion():
    s = _seg(" ".join(f"w{i}" for i in range(30)), 0.0, paso=0.8)
    partes = partir_para_traducir([s])
    assert len(partes) >= 2
    assert all(p.fin - p.ini <= 12.0 + 1e-6 for p in partes)


def test_segmento_sin_palabras_cuenta_como_una():
    s = Segmento(0, 20, "x " * 30, [])
    assert len(partir_para_traducir([s])) == 1


def test_cues_largos_se_parten_y_reparten_tiempo():
    texto = "Entonces ayer iba caminando por la calle y se me acerca un tipo que me quiere vender un auto sin ruedas."
    cues = armar_cues([(0.0, 6.0, texto)])
    assert len(cues) == 2
    assert all(len(c.texto) <= MAX_CHARS_CUE + 15 for c in cues)
    assert cues[0].ini == 0.0 and abs(cues[-1].fin - 6.0) < 1e-6
    assert " ".join(c.texto for c in cues) == texto


def test_ajustar_sin_solapamientos_y_duracion_minima():
    cues = ajustar([Cue(0.0, 0.2, "a"), Cue(0.5, 2.0, "b"), Cue(1.5, 3.0, "c"), Cue(10, 11, "")])
    assert [c.texto for c in cues] == ["a", "b", "c"]
    assert cues[0].fin == 0.5  # alargado hasta el siguiente, sin pisarlo
    assert cues[1].fin <= cues[2].ini
    for c in cues:
        assert c.fin > c.ini


def test_vtt():
    vtt = a_vtt([Cue(1.5, 3.25, "Hola --> che")])
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.500 --> 00:00:03.250" in vtt
    assert "Hola → che" in vtt
