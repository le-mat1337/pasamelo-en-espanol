"""La API de punta a punta con dependencias falsas: sin red, sin Groq, sin Blob."""

import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as modulo_app
from pes import config as configuracion
from pes.descarga import Descarga
from pes.errores import error
from pes.jobs import Deps
from pes.storage import StoreMemoria
from pes.transcribe import Palabra, Segmento, Transcripcion
from pes.urls import resolver

IG = "https://www.instagram.com/reel/Cab12_-xyZ{n}/"


class Reloj:
    def __init__(self):
        self.t = 1_790_000_000.0  # 2026

    def __call__(self):
        return self.t


class VideosFalsos:
    def __init__(self):
        self.subidos: list[str] = []

    async def subir(self, ruta: Path, job_id: str) -> str:
        assert ruta.is_file()
        self.subidos.append(job_id)
        return f"https://store.public.blob.vercel-storage.com/videos/{job_id}-abc.mp4"

    def url(self, ref, expira, ahora):
        return ref

    async def borrar_huerfanos(self, store, ahora, vida_max_s):
        return 0


def _transcripcion(idioma="english"):
    palabras = [Palabra("Hello", 0.0, 0.4), Palabra("my", 0.5, 0.7), Palabra("friend.", 0.8, 1.2)]
    return Transcripcion(idioma, 3.0, [Segmento(0.0, 1.2, "Hello my friend.", palabras)])


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    reloj = Reloj()
    store = StoreMemoria(reloj)
    videos = VideosFalsos()
    estado = {"idioma": "english", "error_descarga": None, "captcha_ok": True, "descargas": 0}

    def descargar(ref, carpeta, max_mb, max_dur):
        estado["descargas"] += 1
        if estado["error_descarga"]:
            raise estado["error_descarga"]
        video = carpeta / "video.mp4"
        audio = carpeta / "audio.mp3"
        video.write_bytes(b"v" * 100)
        audio.write_bytes(b"a" * 2000)
        return Descarga(video, audio, 3.0, "alguien", 100)

    async def transcribir(audio):
        return _transcripcion(estado["idioma"])

    async def traducir(textos):
        return [f"ES {t}" for t in textos]

    async def verificar_humano(token, ip):
        if not estado["captcha_ok"]:
            raise error("captcha", http=403)

    cfg = dataclasses.replace(
        configuracion.cargar(),
        entorno="development",
        groq_api_key="gsk_test",
        traductor="groq",
        pausado=False,
        max_por_ip_dia=3,
        max_jobs_dia=300,
        max_jobs_hora=40,
        cron_secret="secreto-cron",
    )
    deps = Deps(
        cfg=cfg,
        store=store,
        videos=videos,
        resolver=resolver,
        verificar_humano=verificar_humano,
        descargar=descargar,
        transcribir=transcribir,
        traducir=traducir,
        reloj=reloj,
    )
    modulo_app.app.dependency_overrides[modulo_app.obtener_deps] = lambda: deps
    cliente = TestClient(modulo_app.app)
    yield cliente, deps, estado, reloj
    modulo_app.app.dependency_overrides.clear()


def _pedir(cliente, n=1, ip="200.1.2.3", **extra):
    resp = cliente.post(
        "/api/jobs", json={"url": IG.format(n=n), "turnstile": "tok", **extra}, headers={"x-real-ip": ip}
    )
    if resp.headers["content-type"].startswith("application/x-ndjson"):
        return resp, [json.loads(linea) for linea in resp.text.splitlines() if linea]
    return resp, None


def test_camino_feliz(entorno):
    cliente, deps, _, _ = entorno
    resp, eventos = _pedir(cliente)
    assert resp.status_code == 200
    etapas = [e["etapa"] for e in eventos]
    assert etapas == ["aceptado", "descargando", "escuchando", "traduciendo", "guardando", "listo"]
    job_id = eventos[-1]["id"]
    assert eventos[-1]["url"] == f"/v/{job_id}"

    job = cliente.get(f"/api/jobs/{job_id}")
    assert job.status_code == 200
    datos = job.json()
    assert datos["estado"] == "listo"
    assert datos["cues"] and datos["cues"][0]["t"] == "ES Hello my friend."
    assert datos["video_url"].startswith("https://")
    assert "video_id" not in datos and "creado" not in datos  # solo campos públicos
    assert "public" in job.headers["cache-control"]
    assert job.headers["x-content-type-options"] == "nosniff"


def test_mismo_video_sale_de_cache_y_no_gasta_cupo(entorno):
    cliente, deps, estado, _ = entorno
    _, eventos = _pedir(cliente)
    resp, _ = _pedir(cliente, ip="9.9.9.9")
    assert resp.status_code == 200
    assert resp.json() == {"etapa": "listo", "id": eventos[-1]["id"], "url": f"/v/{eventos[-1]['id']}"}
    assert estado["descargas"] == 1
    for n in (2, 3, 4):  # la IP 9.9.9.9 sigue teniendo sus 3
        resp, eventos = _pedir(cliente, n=n, ip="9.9.9.9")
        assert eventos[-1]["etapa"] == "listo"


def test_cupo_por_ip(entorno):
    cliente, *_ = entorno
    for n in (1, 2, 3):
        _, eventos = _pedir(cliente, n=n)
        assert eventos[-1]["etapa"] == "listo"
    resp, _ = _pedir(cliente, n=4)
    assert resp.status_code == 429 and resp.json()["codigo"] == "cupo_ip"
    resp, eventos = _pedir(cliente, n=4, ip="200.1.2.4")  # otra IP sí puede
    assert eventos[-1]["etapa"] == "listo"


def test_cupo_se_renueva_al_dia_siguiente(entorno):
    cliente, _, _, reloj = entorno
    for n in (1, 2, 3):
        _pedir(cliente, n=n)
    reloj.t += 86400
    _, eventos = _pedir(cliente, n=5)
    assert eventos[-1]["etapa"] == "listo"


def test_ipv6_se_agrupa_por_64(entorno):
    cliente, *_ = entorno
    for n, ip in enumerate(("2001:db8:1:1::1", "2001:db8:1:1::2", "2001:db8:1:1:abcd::9"), start=1):
        _pedir(cliente, n=n, ip=ip)
    resp, _ = _pedir(cliente, n=4, ip="2001:db8:1:1:ffff::1")
    assert resp.status_code == 429


def test_un_job_por_vez(entorno):
    cliente, deps, *_ = entorno
    import asyncio

    from pes.limites import fecha_local, hash_ip

    h = hash_ip("200.1.2.3", deps.cfg.sal, fecha_local(deps.reloj()))
    asyncio.run(deps.store.set(f"rl:activo:{h}", "1", ex=300))
    resp, _ = _pedir(cliente)
    assert resp.status_code == 429 and resp.json()["codigo"] == "en_curso"


def test_tope_global_por_hora(entorno):
    cliente, deps, *_ = entorno
    deps.cfg = dataclasses.replace(deps.cfg, max_jobs_hora=1)
    _pedir(cliente, n=1)
    resp, _ = _pedir(cliente, n=2, ip="1.1.1.1")
    assert resp.status_code == 429 and resp.json()["codigo"] == "cupo_hora"


def test_link_invalido_no_toca_cupos(entorno):
    cliente, deps, *_ = entorno
    antes = deps.store.comandos
    resp = cliente.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=x"})
    assert resp.status_code == 400 and resp.json()["codigo"] == "link_invalido"
    assert deps.store.comandos == antes


def test_validacion_no_filtra_detalles(entorno):
    cliente, *_ = entorno
    resp = cliente.post("/api/jobs", json={"nada": 1})
    assert resp.status_code == 400 and set(resp.json()) == {"codigo", "mensaje"}


def test_cuerpo_gigante(entorno):
    cliente, *_ = entorno
    resp = cliente.post("/api/jobs", content=b"x" * 20000, headers={"content-type": "application/json"})
    assert resp.status_code == 413


def test_captcha_invalido_no_gasta(entorno):
    cliente, _, estado, _ = entorno
    estado["captcha_ok"] = False
    resp, _ = _pedir(cliente)
    assert resp.status_code == 403 and resp.json()["codigo"] == "captcha"
    assert estado["descargas"] == 0


def test_no_ingles_consume_cupo_y_permite_reintentar(entorno):
    cliente, deps, estado, _ = entorno
    estado["idioma"] = "spanish"
    _, eventos = _pedir(cliente)
    assert eventos[-1]["etapa"] == "error" and eventos[-1]["codigo"] == "no_ingles"
    estado["idioma"] = "english"
    _, eventos = _pedir(cliente)  # el mismo video se puede volver a pedir
    assert eventos[-1]["etapa"] == "listo"
    _pedir(cliente, n=2)
    resp, _ = _pedir(cliente, n=3)
    assert resp.status_code == 429  # el error de idioma sí contó


def test_error_nuestro_devuelve_cupo(entorno):
    cliente, _, estado, _ = entorno
    estado["error_descarga"] = error("bloqueado", http=503, devolver_cupo=True)
    for _ in range(5):
        _, eventos = _pedir(cliente)
        assert eventos[-1]["codigo"] == "bloqueado"
    estado["error_descarga"] = None
    _, eventos = _pedir(cliente)
    assert eventos[-1]["etapa"] == "listo"


def test_excepcion_inesperada_no_filtra(entorno):
    cliente, _, estado, _ = entorno
    estado["error_descarga"] = RuntimeError("/tmp/secreto stack trace")
    _, eventos = _pedir(cliente)
    assert eventos[-1]["codigo"] == "interno"
    assert "secreto" not in json.dumps(eventos)


def test_pausado(entorno):
    cliente, deps, *_ = entorno
    deps.cfg = dataclasses.replace(deps.cfg, pausado=True)
    resp, _ = _pedir(cliente)
    assert resp.status_code == 503 and resp.json()["codigo"] == "pausado"


def test_produccion_sin_config_no_procesa(entorno):
    cliente, deps, *_ = entorno
    deps.cfg = dataclasses.replace(deps.cfg, entorno="production", turnstile_secret="", sal="x", cron_secret="y")
    resp, _ = _pedir(cliente)
    assert resp.status_code == 503 and resp.json()["codigo"] == "config"
    assert "TURNSTILE" not in resp.text


def test_job_inexistente_y_id_raro(entorno):
    cliente, *_ = entorno
    assert cliente.get("/api/jobs/AAAAAAAAAAAAAAAA").status_code == 404
    assert cliente.get("/api/jobs/..%2F..%2Fetc").status_code == 404
    assert cliente.get("/api/jobs/" + "a" * 500).status_code == 404


def test_job_vencido(entorno):
    cliente, _, _, reloj = entorno
    _, eventos = _pedir(cliente)
    reloj.t += 25 * 3600
    resp = cliente.get(f"/api/jobs/{eventos[-1]['id']}")
    assert resp.status_code == 404 and resp.json()["codigo"] == "no_encontrado"


def test_job_cortado(entorno):
    cliente, deps, _, reloj = entorno
    import asyncio

    from pes.jobs import guardar, nuevo_job
    from pes.urls import parsear

    job = nuevo_job("BBBBBBBBBBBBBBBB", parsear(IG.format(n=1)), reloj(), 86400)
    asyncio.run(guardar(deps.store, job, reloj()))
    reloj.t += 400
    datos = cliente.get("/api/jobs/BBBBBBBBBBBBBBBB").json()
    assert datos["estado"] == "error" and datos["codigo"] == "cortado"


def test_cron_necesita_secreto(entorno):
    cliente, *_ = entorno
    assert cliente.get("/api/cron/limpiar").status_code == 401
    assert cliente.get("/api/cron/limpiar", headers={"authorization": "Bearer otro"}).status_code == 401
    ok = cliente.get("/api/cron/limpiar", headers={"authorization": "Bearer secreto-cron"})
    assert ok.status_code == 200 and ok.json() == {"borrados": 0}


def test_salud_no_revela_secretos(entorno):
    cliente, *_ = entorno
    datos = cliente.get("/api/salud").json()
    assert set(datos) == {"ok", "pausado", "configurado"}


def test_sin_docs_publicas(entorno):
    cliente, *_ = entorno
    for ruta in ("/docs", "/redoc", "/openapi.json"):
        assert cliente.get(ruta).status_code == 404


def test_cache_apuntando_a_job_roto_se_reintenta(entorno):
    cliente, deps, _, reloj = entorno
    import asyncio

    from pes.urls import parsear

    ref = parsear(IG.format(n=1))
    asyncio.run(deps.store.set(f"video:{ref.clave}", "ZZZZZZZZZZZZZZZZ", ex=3600))  # job inexistente
    _, eventos = _pedir(cliente)
    assert eventos[-1]["etapa"] == "listo"


def test_pipeline_con_limite_de_tiempo(entorno):
    cliente, deps, estado, _ = entorno
    import time as _time

    deps.cfg = dataclasses.replace(deps.cfg, limite_job_s=1)

    async def lento(audio):
        import asyncio

        await asyncio.sleep(3)

    deps.transcribir = lento
    t0 = _time.monotonic()
    _, eventos = _pedir(cliente)
    assert eventos[-1]["codigo"] == "interno"
    assert _time.monotonic() - t0 < 3
    deps.transcribir = lambda audio: _devolver(_transcripcion())
    for n in (2, 3, 4):  # el cupo se devolvió: siguen quedando 3
        _, eventos = _pedir(cliente, n=n)
        assert eventos[-1]["etapa"] == "listo"


async def _devolver(valor):
    return valor


def test_logs_sin_urls_de_httpx():
    import logging

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
