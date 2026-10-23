import pytest

from pes.storage import StoreMemoria, VideosLocales

pytestmark = pytest.mark.anyio


class Reloj:
    t = 1000.0

    def __call__(self):
        return self.t


async def test_memoria_ttl_nx_incr():
    r = Reloj()
    s = StoreMemoria(r)
    assert await s.set("a", "1", ex=10, nx=True)
    assert not await s.set("a", "2", nx=True)
    assert await s.incr("c") == 1 and await s.incr("c") == 2 and await s.decr("c") == 1
    r.t += 11
    assert await s.get("a") is None
    assert await s.mget(["a", "c"]) == [None, "1"]


async def test_videos_locales_borra_huerfanos(tmp_path):
    r = Reloj()
    store = StoreMemoria(r)
    videos = VideosLocales(tmp_path / "v")
    origen = tmp_path / "x.mp4"
    origen.write_bytes(b"x")
    await videos.subir(origen, "AAAAAAAAAAAAAAAA")
    await videos.subir(origen, "BBBBBBBBBBBBBBBB")
    await store.set("job:AAAAAAAAAAAAAAAA", "{}", ex=100)
    import os
    import time

    viejo = time.time() - 120
    for f in (tmp_path / "v").glob("*.mp4"):
        os.utime(f, (viejo, viejo))
    borrados = await videos.borrar_huerfanos(store, time.time(), 72 * 3600)
    assert borrados == 1
    assert [f.stem for f in (tmp_path / "v").glob("*.mp4")] == ["AAAAAAAAAAAAAAAA"]
