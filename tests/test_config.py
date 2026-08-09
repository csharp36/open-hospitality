from usali.config import Settings


def test_settings_default_db_url_is_postgres():
    s = Settings()
    assert s.db_url.startswith("postgresql+psycopg://")


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("USALI_DB_URL", "postgresql+psycopg://x:y@h:1/db")
    assert Settings().db_url == "postgresql+psycopg://x:y@h:1/db"
