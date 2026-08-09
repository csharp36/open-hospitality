from usali.db import make_engine, make_session_factory


def test_make_engine_uses_url():
    eng = make_engine("postgresql+psycopg://u:p@localhost:5432/db")
    assert eng.url.drivername == "postgresql+psycopg"


def test_session_factory_returns_callable():
    eng = make_engine("postgresql+psycopg://u:p@localhost:5432/db")
    factory = make_session_factory(eng)
    assert callable(factory)
