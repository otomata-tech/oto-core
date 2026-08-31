"""Secret resolution semantics (oto.config.get_secret + oto.secrets factory).

Locks the subtle rule that the file fallback fires ONLY when the configured
provider's store is ABSENT — not when the store is present but lacks the key.

Also locks the oto-core#63 fixes: a store-absent configured provider warns
ONCE per process (never silent, never per-lookup spam), and an unknown
`secret_provider` name raises instead of silently becoming `file`.
"""
import warnings

import pytest

import oto.config as config
from oto.secrets import MISSING, STORE_ABSENT, AmbiguousSecretError, make_provider
from oto.secrets.file import FileProvider


class _Stub:
    def __init__(self, result, *, exists=True):
        self._result = result
        self._exists = exists

    def lookup(self, name):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def store_exists(self):
        return self._exists


@pytest.fixture(autouse=True)
def _reset_warned_once():
    """`_warned_store_absent` is process-global by design (warn once ever,
    not once per test) — reset it so tests don't depend on run order."""
    config._warned_store_absent.clear()
    yield
    config._warned_store_absent.clear()


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


def test_factory_unknown_raises(monkeypatch):
    """A typo'd `secret_provider` (oto-core#63) must be a NAMED failure, not a
    silent swap to the file provider — that swap is what made Alexis's
    `secret_provider: file` incident indistinguishable from "all keys unset"."""
    with pytest.raises(ValueError, match="Unknown secret provider 'nope'"):
        make_provider("nope", {})


def test_store_absent_warns_once_per_process(monkeypatch):
    """The configured provider (not the fallback) is checked. First call warns;
    a second call for a DIFFERENT key must NOT warn again."""
    monkeypatch.delenv("OTO_CONFIG_DISABLE_SOPS", raising=False)
    monkeypatch.delenv("W1", raising=False)
    monkeypatch.delenv("W2", raising=False)
    _wire(monkeypatch, _Stub(STORE_ABSENT, exists=False), file_result=MISSING,
          config_name="file")

    with pytest.warns(RuntimeWarning, match="fournisseur de secrets 'file'"):
        config.get_secret("W1", default="d")

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning here fails the test
        config.get_secret("W2", default="d")


def test_store_present_never_warns(monkeypatch):
    monkeypatch.delenv("OTO_CONFIG_DISABLE_SOPS", raising=False)
    monkeypatch.delenv("W3", raising=False)
    _wire(monkeypatch, _Stub(MISSING, exists=True), config_name="sops")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert config.get_secret("W3", default="d") == "d"


def test_file_provider_store_exists(tmp_path, monkeypatch):
    from oto.secrets.file import FileProvider

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert FileProvider().store_exists() is False

    (tmp_path / ".otomata").mkdir()
    (tmp_path / ".otomata" / "secrets.env").write_text("X=1\n")
    assert FileProvider().store_exists() is True


def test_sops_provider_store_exists(tmp_path, monkeypatch):
    from oto.secrets.sops import SopsProvider

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert SopsProvider({}).store_exists() is False

    sops_dir = tmp_path / ".otomata" / "secrets"
    sops_dir.mkdir(parents=True)
    (sops_dir / ".sops.yaml").write_text("creation_rules: []\n")
    assert SopsProvider({}).store_exists() is True


def test_scaleway_provider_store_exists(tmp_path, monkeypatch):
    import oto.secrets.scaleway as scw

    monkeypatch.setattr(scw, "_SCW_CONFIG", tmp_path / "config.yaml")
    assert scw.ScalewayProvider().store_exists() is False

    (tmp_path / "config.yaml").write_text("access_key: x\n")
    assert scw.ScalewayProvider().store_exists() is True
