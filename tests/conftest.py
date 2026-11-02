import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Ninguna prueba debe tocar la carpeta de datos real del usuario."""
    data = tmp_path / "datos"
    data.mkdir()
    monkeypatch.setenv("MIKOSHI_DATA", str(data))
