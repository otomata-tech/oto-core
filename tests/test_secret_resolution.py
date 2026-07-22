"""Secret resolution semantics (oto.config.get_secret + oto.secrets factory).

Locks the subtle rule that the file fallback fires ONLY when the configured
provider's store is ABSENT — not when the store is present but lacks the key.
"""
import oto.config as config
from oto.secrets import MISSING, STORE_ABSENT, AmbiguousSecretError, make_provider
from oto.secrets.file import FileProvider


class _Stub:
    def __init__(self, result):
        self._result = result

    def lookup(self, name):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _wire(monkeypatch, provider, *, file_result=MISSING, config_name="sops"):
    monkeypatch.setattr(config, "get_provider", lambda: config_name)
    monkeypatch.setattr(config, "_get_oto_config", lambda: {})
    monkeypatch.setattr(config, "make_provider", lambda name, cfg: provider)
    monkeypatch.setattr(FileProvider, "lookup", lambda self, name: file_result)


def test_env_wins(monkeypatch):
    monkeypatch.setenv("OTO_SECRET_X", "from-env")
    # Even a provider that would return something else must be ignored.
    _wire(monkeypatch, _Stub("from-provider"))
    assert config.get_secret("OTO_SECRET_X") == "from-env"


def test_disable_sops_returns_default(monkeypatch):
    monkeypatch.delenv("OTO_SECRET_Y", raising=False)
    monkeypatch.setenv("OTO_CONFIG_DISABLE_SOPS", "1")
    _wire(monkeypatch, _Stub("should-not-be-read"))
    assert config.get_secret("OTO_SECRET_Y", default="d") == "d"


def test_provider_value(monkeypatch):
    monkeypatch.delenv("OTO_CONFIG_DISABLE_SOPS", raising=False)
    monkeypatch.delenv("OTO_SECRET_Z", raising=False)
    _wire(monkeypatch, _Stub("from-sops"))
    assert config.get_secret("OTO_SECRET_Z") == "from-sops"


def test_key_absent_store_present_no_fallback(monkeypatch):
    """Store present + key missing → default, WITHOUT touching the file provider."""
    monkeypatch.delenv("OTO_CONFIG_DISABLE_SOPS", raising=False)
    monkeypatch.delenv("K", raising=False)
    _wire(monkeypatch, _Stub(MISSING), file_result="file-value-should-be-ignored")
    assert config.get_secret("K", default="d") == "d"


def test_store_absent_falls_back_to_file(monkeypatch):
    """Store absent → fall back to the local file provider."""
    monkeypatch.delenv("OTO_CONFIG_DISABLE_SOPS", raising=False)
    monkeypatch.delenv("K2", raising=False)
    _wire(monkeypatch, _Stub(STORE_ABSENT), file_result="from-file")
    assert config.get_secret("K2") == "from-file"


def test_store_absent_and_file_absent_returns_default(monkeypatch):
    monkeypatch.delenv("OTO_CONFIG_DISABLE_SOPS", raising=False)
    monkeypatch.delenv("K3", raising=False)
    _wire(monkeypatch, _Stub(STORE_ABSENT), file_result=MISSING)
    assert config.get_secret("K3", default="d") == "d"


def test_ambiguous_raises(monkeypatch):
    monkeypatch.delenv("OTO_CONFIG_DISABLE_SOPS", raising=False)
    monkeypatch.delenv("DBURL", raising=False)
    _wire(monkeypatch, _Stub(AmbiguousSecretError("scoped per file")))
    try:
        config.get_secret("DBURL")
    except AmbiguousSecretError:
        return
    raise AssertionError("expected AmbiguousSecretError")


def test_factory_unknown_falls_back_to_file():
    assert isinstance(make_provider("nope", {}), FileProvider)
