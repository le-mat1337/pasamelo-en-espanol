from pes import config


def test_env_local_no_pisa(tmp_path, monkeypatch):
    archivo = tmp_path / ".env"
    archivo.write_text('# comentario\nPES_X_NUEVA="hola"\nPES_X_EXISTE=archivo\nPES_X_VACIA=\nbasura\n')
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("PES_X_NUEVA", raising=False)
    monkeypatch.setenv("PES_X_EXISTE", "entorno")
    config.cargar_env_local(archivo)
    import os

    assert os.environ["PES_X_NUEVA"] == "hola"
    assert os.environ["PES_X_EXISTE"] == "entorno"
    assert "PES_X_VACIA" not in os.environ
    monkeypatch.delenv("PES_X_NUEVA")


def test_env_local_no_corre_en_vercel(tmp_path, monkeypatch):
    archivo = tmp_path / ".env"
    archivo.write_text("PES_X_VERCEL=1\n")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("PES_X_VERCEL", raising=False)
    config.cargar_env_local(archivo)
    import os

    assert "PES_X_VERCEL" not in os.environ


def test_demo_nunca_en_produccion(monkeypatch):
    monkeypatch.setenv("PES_DEMO", "1")
    monkeypatch.setenv("VERCEL_ENV", "production")
    assert config.cargar().demo is False
    monkeypatch.setenv("VERCEL_ENV", "development")
    assert config.cargar().demo is True


def test_sal_obligatoria_en_produccion(monkeypatch):
    monkeypatch.delenv("PES_SAL", raising=False)
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "x")
    monkeypatch.setenv("CRON_SECRET", "x")
    assert config.cargar().faltantes() == ["PES_SAL"]
    monkeypatch.setenv("PES_SAL", "algo")
    assert config.cargar().faltantes() == []
