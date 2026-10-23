"""R2: firma SigV4 contra el ejemplo oficial de AWS y el store de videos con HTTP falso."""

import asyncio
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from pes.errores import PesError
from pes.r2 import CredencialesR2, firmar_url, parsear_listado
from pes.storage import StoreMemoria, VideosR2, crear_videos

CUENTA = "0123456789abcdef0123456789abcdef"
ENV = {"R2_ACCOUNT_ID": CUENTA, "R2_ACCESS_KEY_ID": "AK", "R2_SECRET_ACCESS_KEY": "SK", "R2_BUCKET": "pes-videos"}


def test_firma_igual_al_ejemplo_de_aws():
    # "Authenticating Requests: Using Query Parameters (AWS Signature Version 4)", ejemplo GET
    url = firmar_url(
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/test.txt",
        region="us-east-1",
        acceso="AKIAIOSFODNN7EXAMPLE",
        secreto="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        ahora=datetime(2013, 5, 24, tzinfo=UTC),
        expira_s=86400,
    )
    assert url == (
        "https://examplebucket.s3.amazonaws.com/test.txt"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )


def test_credenciales_desde_env():
    assert CredencialesR2.desde_env({}) is None
    cred = CredencialesR2.desde_env(ENV)
    assert cred.host == f"{CUENTA}.r2.cloudflarestorage.com"
    assert "SK" not in repr(cred)


@pytest.mark.parametrize(
    "cambio",
    [{"R2_SECRET_ACCESS_KEY": ""}, {"R2_ACCOUNT_ID": "evil.com/x"}, {"R2_BUCKET": "Bucket_Raro"}],
)
def test_credenciales_a_medias_o_raras_son_error_de_config(cambio):
    with pytest.raises(PesError) as e:
        CredencialesR2.desde_env({**ENV, **cambio})
    assert e.value.codigo == "config"


def test_crear_videos_prefiere_r2(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_x")
    assert isinstance(crear_videos(produccion=True), VideosR2)


def _listado(claves_y_fechas, token=None):
    contenidos = "".join(
        f"<Contents><Key>{c}</Key><LastModified>{f}</LastModified><Size>1</Size></Contents>" for c, f in claves_y_fechas
    )
    resto = (
        f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>"
        if token
        else ("<IsTruncated>false</IsTruncated>")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{contenidos}{resto}</ListBucketResult>'
    ).encode()


def test_parsear_listado():
    objetos, token = parsear_listado(_listado([("videos/a.mp4", "2026-10-22T10:00:00.000Z")], token="t2"))
    assert [o.clave for o in objetos] == ["videos/a.mp4"]
    assert objetos[0].modificado == datetime(2026, 10, 22, 10, tzinfo=UTC).timestamp()
    assert token == "t2"


class R2Falso:
    def __init__(self, paginas=()):
        self.pedidos: list[httpx.Request] = []
        self.paginas = list(paginas)
        self.estado_put = 200

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        q = parse_qs(urlsplit(str(pedido.url)).query)
        assert pedido.url.host == f"{CUENTA}.r2.cloudflarestorage.com"
        assert q["X-Amz-Signature"] and q["X-Amz-SignedHeaders"] == ["host"]
        if pedido.method == "PUT":
            return httpx.Response(self.estado_put)
        if pedido.method == "GET":
            return httpx.Response(200, content=self.paginas.pop(0))
        return httpx.Response(204)


def _videos(falso):
    return VideosR2(CredencialesR2.desde_env(ENV), httpx.AsyncClient(transport=httpx.MockTransport(falso)))


def test_subir_y_url_firmada(tmp_path):
    falso = R2Falso()
    archivo = tmp_path / "video.mp4"
    archivo.write_bytes(b"mp4")
    videos = _videos(falso)
    ref = asyncio.run(videos.subir(archivo, "A" * 16))
    assert ref == f"videos/{'A' * 16}.mp4"
    put = falso.pedidos[0]
    assert put.method == "PUT" and put.url.path == f"/pes-videos/videos/{'A' * 16}.mp4"
    assert put.headers["content-type"] == "video/mp4" and put.content == b"mp4"

    ahora = 1_790_000_000.0
    q = parse_qs(urlsplit(videos.url(ref, ahora + 3600, ahora)).query)
    assert q["X-Amz-Expires"] == ["3600"]
    q = parse_qs(urlsplit(videos.url(ref, ahora + 30 * 86400, ahora)).query)
    assert q["X-Amz-Expires"] == [str(7 * 86400)]


def test_subir_falla_devuelve_cupo(tmp_path):
    falso = R2Falso()
    falso.estado_put = 403
    archivo = tmp_path / "video.mp4"
    archivo.write_bytes(b"mp4")
    with pytest.raises(PesError) as e:
        asyncio.run(_videos(falso).subir(archivo, "A" * 16))
    assert e.value.codigo == "interno" and e.value.devolver_cupo


def test_borrar_huerfanos_pagina_y_respeta_jobs_vivos():
    ahora = datetime(2026, 10, 22, 12, tzinfo=UTC).timestamp()
    vivo, muerto, viejo = "V" * 16, "M" * 16, "O" * 16
    falso = R2Falso(
        [
            _listado(
                [
                    (f"videos/{vivo}.mp4", "2026-10-22T09:00:00.000Z"),  # 3 h, job vivo: queda
                    (f"videos/{muerto}.mp4", "2026-10-22T09:00:00.000Z"),  # 3 h, job vencido: se borra
                ],
                token="pagina2",
            ),
            _listado(
                [
                    (f"videos/{viejo}.mp4", "2026-10-18T12:00:00.000Z"),  # 4 días: se borra aunque viva
                    ("videos/otra-cosa.mp4", "2026-10-22T11:30:00.000Z"),  # 30 min: queda
                ]
            ),
        ]
    )
    store = StoreMemoria(lambda: ahora)
    asyncio.run(store.set(f"job:{vivo}", "{}"))
    asyncio.run(store.set(f"job:{viejo}", "{}"))
    borrados = asyncio.run(_videos(falso).borrar_huerfanos(store, ahora, 72 * 3600))
    assert borrados == 2
    listados = [p for p in falso.pedidos if p.method == "GET"]
    assert "continuation-token=pagina2" in str(listados[1].url)
    assert sorted(p.url.path for p in falso.pedidos if p.method == "DELETE") == [
        f"/pes-videos/videos/{muerto}.mp4",
        f"/pes-videos/videos/{viejo}.mp4",
    ]
