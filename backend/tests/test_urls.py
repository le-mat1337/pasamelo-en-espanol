import httpx
import pytest

from pes.errores import PesError
from pes.urls import LinkCorto, VideoRef, extraer_url, parsear, resolver

pytestmark = pytest.mark.anyio

# IDs inventados: el repo es público y no lleva links de videos de terceros.
IG = "Cab12_-xyZ9"
TT = "7400000000000000001"


@pytest.mark.parametrize(
    "texto",
    [
        f"https://www.instagram.com/reel/{IG}/",
        f"https://instagram.com/reel/{IG}",
        f"https://www.instagram.com/reels/{IG}/?igsh=abc123",
        f"https://www.instagram.com/p/{IG}/?utm_source=ig_web_copy_link",
        f"https://www.instagram.com/alguien.123/reel/{IG}/",
        f"http://m.instagram.com/tv/{IG}",
        f"Mirá esto jajaja https://www.instagram.com/reel/{IG}/?igsh=x buenísimo",
        f"instagram.com/reel/{IG}",
        f"HTTPS://WWW.INSTAGRAM.COM/reel/{IG}/",
    ],
)
def test_instagram_valido(texto):
    ref = parsear(extraer_url(texto))
    assert ref == VideoRef("instagram", IG, f"https://www.instagram.com/reel/{IG}/")


@pytest.mark.parametrize(
    "texto",
    [
        f"https://www.tiktok.com/@alguien/video/{TT}",
        f"https://www.tiktok.com/@al.gui_en-1/video/{TT}?is_from_webapp=1&sender_device=pc",
        f"https://tiktok.com/@alguien/video/{TT}/",
    ],
)
def test_tiktok_valido(texto):
    ref = parsear(extraer_url(texto))
    assert isinstance(ref, VideoRef)
    assert (ref.plataforma, ref.id) == ("tiktok", TT)
    assert ref.url.startswith("https://www.tiktok.com/@")
    assert "?" not in ref.url


def test_tiktok_movil():
    assert parsear(f"https://m.tiktok.com/v/{TT}.html") == VideoRef("tiktok", TT, f"https://m.tiktok.com/v/{TT}.html")


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("https://vm.tiktok.com/ZMabc123/", "https://vm.tiktok.com/ZMabc123/"),
        ("https://vt.tiktok.com/ZSxyz789", "https://vt.tiktok.com/ZSxyz789/"),
        ("https://www.tiktok.com/t/ZTabc123/", "https://www.tiktok.com/t/ZTabc123/"),
        ("https://www.instagram.com/share/reel/BAabc123", "https://www.instagram.com/share/reel/BAabc123"),
    ],
)
def test_links_cortos(texto, esperado):
    assert parsear(extraer_url(texto)) == LinkCorto(esperado)


@pytest.mark.parametrize(
    "texto",
    [
        "",
        "   ",
        "hola",
        "https://www.youtube.com/shorts/abc",
        "https://evil.com/www.instagram.com/reel/abc12/",
        "https://www.instagram.com.evil.com/reel/abcde/",
        "https://instagram.com@evil.com/reel/abcde/",
        "https://user:pass@www.instagram.com/reel/abcde/",
        "https://www.instagram.com:8443/reel/abcde/",
        "ftp://www.instagram.com/reel/abcde/",
        "javascript:alert(1)//www.instagram.com/reel/abcde",
        "file:///etc/passwd",
        "https://127.0.0.1/reel/abcde/",
        "https://169.254.169.254/latest/meta-data/",
        "https://www.instagram.com/alguien/",
        "https://www.instagram.com/reel/../../etc/passwd",
        "https://www.instagram.com/reel/abc/",  # código demasiado corto
        "https://www.tiktok.com/@alguien",
        "https://www.tiktok.com/@alguien/video/123",
        "https://www.tiktok.com/@alguien/video/7400000000000000001;rm -rf /",
        "https://vm.tiktok.com/../../x",
        "https://www.instagram.com/reel/" + "a" * 3000,
        "https://xn--nstagram-6ya.com/reel/abcde/",  # homógrafo
    ],
)
def test_invalidos(texto):
    with pytest.raises(PesError) as e:
        parsear(extraer_url(texto))
    assert e.value.codigo == "link_invalido"


def test_carrusel_tiktok():
    with pytest.raises(PesError) as e:
        parsear(f"https://www.tiktok.com/@alguien/photo/{TT}")
    assert e.value.codigo == "carrusel"


def test_no_string():
    with pytest.raises(PesError):
        extraer_url(None)  # type: ignore[arg-type]


def _cliente(rutas: dict[str, tuple[int, str | None]]) -> httpx.AsyncClient:
    pedidos: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        pedidos.append(url)
        status, location = rutas.get(url, (404, None))
        headers = {"location": location} if location else {}
        return httpx.Response(status, headers=headers)

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cliente.pedidos = pedidos  # type: ignore[attr-defined]
    return cliente


async def test_resolver_tiktok_corto():
    cliente = _cliente({"https://vm.tiktok.com/ZMabc123/": (301, f"https://www.tiktok.com/@alguien/video/{TT}?_r=1")})
    ref = await resolver("https://vm.tiktok.com/ZMabc123/", cliente)
    assert ref == VideoRef("tiktok", TT, f"https://www.tiktok.com/@alguien/video/{TT}")


async def test_resolver_instagram_share_relativo():
    cliente = _cliente({"https://www.instagram.com/share/reel/BAabc123": (302, f"/reel/{IG}/?igsh=1")})
    ref = await resolver("https://www.instagram.com/share/reel/BAabc123", cliente)
    assert ref.id == IG


async def test_resolver_no_sigue_hosts_ajenos():
    cliente = _cliente({"https://vm.tiktok.com/ZMabc123/": (302, "http://169.254.169.254/latest/meta-data/")})
    with pytest.raises(PesError) as e:
        await resolver("https://vm.tiktok.com/ZMabc123/", cliente)
    assert e.value.codigo == "link_invalido"
    assert cliente.pedidos == ["https://vm.tiktok.com/ZMabc123/"]  # nunca tocó el host ajeno


async def test_resolver_corta_bucles():
    cliente = _cliente(
        {
            "https://vm.tiktok.com/ZMaaaaa/": (302, "https://vm.tiktok.com/ZMbbbbb/"),
            "https://vm.tiktok.com/ZMbbbbb/": (302, "https://vm.tiktok.com/ZMaaaaa/"),
        }
    )
    with pytest.raises(PesError) as e:
        await resolver("https://vm.tiktok.com/ZMaaaaa/", cliente)
    assert e.value.codigo == "no_disponible"
    assert len(cliente.pedidos) == 3


async def test_resolver_directo_no_hace_red():
    cliente = _cliente({})
    await resolver(f"https://www.instagram.com/reel/{IG}/", cliente)
    assert cliente.pedidos == []


async def test_resolver_link_corto_que_lleva_a_la_home():
    cliente = _cliente({"https://vm.tiktok.com/ZMabc123/": (301, "https://www.tiktok.com/")})
    with pytest.raises(PesError) as e:
        await resolver("https://vm.tiktok.com/ZMabc123/", cliente)
    assert e.value.codigo == "no_disponible"
