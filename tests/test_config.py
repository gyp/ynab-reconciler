"""Tests for ynab_reconciler.config."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import keyring
import keyring.errors
import pytest

from ynab_reconciler import config


@pytest.fixture
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config_dir() at a fresh temporary directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / config.APP_NAME


class _StubBackend:
    """In-memory keyring backend for tests."""

    priority = 1  # required attribute on keyring backends

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError as e:
            raise keyring.errors.PasswordDeleteError(str(e)) from e


class _FailingBackend:
    """Keyring backend that raises on every operation."""

    priority = 1

    def get_password(self, service: str, username: str) -> str | None:
        raise keyring.errors.KeyringError("backend unavailable")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.KeyringError("backend unavailable")

    def delete_password(self, service: str, username: str) -> None:
        raise keyring.errors.KeyringError("backend unavailable")


@pytest.fixture
def stub_keyring(monkeypatch: pytest.MonkeyPatch) -> _StubBackend:
    backend = _StubBackend()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


@pytest.fixture
def failing_keyring(monkeypatch: pytest.MonkeyPatch) -> _FailingBackend:
    backend = _FailingBackend()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


# ─── Path resolution ────────────────────────────────────────────────────────


class TestPaths:
    def test_xdg_overrides_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert config.config_dir() == tmp_path / config.APP_NAME
        assert config.config_path() == tmp_path / config.APP_NAME / "config.toml"

    def test_default_is_dotconfig(self, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        assert config.config_dir() == Path.home() / ".config" / config.APP_NAME


# ─── load_config ────────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_missing_file_returns_empty(self, isolated_config_dir):
        cfg = config.load_config()
        assert cfg == config.Config()

    def test_round_trip(self, isolated_config_dir):
        config.save_config(
            config.Config(
                plan_id="p1",
                plans={"p1": config.PlanDefaults(payee_id="p2", category_group_id="g3")},
            )
        )
        cfg = config.load_config()
        assert cfg.plan_id == "p1"
        assert cfg.defaults_for("p1").payee_id == "p2"
        assert cfg.defaults_for("p1").category_group_id == "g3"

    def test_partial_config(self, isolated_config_dir):
        config.save_config(config.Config(plan_id="only-plan"))
        cfg = config.load_config()
        assert cfg.plan_id == "only-plan"
        assert cfg.plans == {}
        assert cfg.defaults_for("only-plan") == config.PlanDefaults()

    def test_multiple_plans_round_trip(self, isolated_config_dir):
        config.save_config(
            config.Config(
                plan_id="p1",
                plans={
                    "p1": config.PlanDefaults(payee_id="pay-1", category_group_id="grp-1"),
                    "p2": config.PlanDefaults(payee_id="pay-2"),
                },
            )
        )
        cfg = config.load_config()
        assert cfg.plan_id == "p1"
        assert cfg.defaults_for("p1") == config.PlanDefaults(
            payee_id="pay-1", category_group_id="grp-1"
        )
        assert cfg.defaults_for("p2") == config.PlanDefaults(payee_id="pay-2")

    def test_malformed_toml_returns_empty(self, isolated_config_dir, capsys):
        isolated_config_dir.mkdir(parents=True)
        config.config_path().write_text("this is not = valid = toml [[")
        cfg = config.load_config()
        assert cfg == config.Config()
        assert "malformed config" in capsys.readouterr().err

    def test_unknown_top_level_key_warned_but_ignored(self, isolated_config_dir, capsys):
        isolated_config_dir.mkdir(parents=True)
        config.config_path().write_text(
            'schema_version = 2\nplan_id = "ok"\nbogus_key = "nope"\n'
        )
        cfg = config.load_config()
        assert cfg.plan_id == "ok"
        err = capsys.readouterr().err
        assert "bogus_key" in err

    def test_unknown_plan_subkey_warned(self, isolated_config_dir, capsys):
        isolated_config_dir.mkdir(parents=True)
        config.config_path().write_text(
            'schema_version = 2\nplan_id = "p1"\n[p1]\npayee_id = "x"\nweird = "y"\n'
        )
        cfg = config.load_config()
        assert cfg.defaults_for("p1").payee_id == "x"
        assert "p1.weird" in capsys.readouterr().err

    def test_non_string_value_ignored(self, isolated_config_dir, capsys):
        isolated_config_dir.mkdir(parents=True)
        config.config_path().write_text(
            'schema_version = 2\nplan_id = 42\n[p1]\npayee_id = 7\n'
        )
        cfg = config.load_config()
        assert cfg.plan_id is None
        assert cfg.defaults_for("p1").payee_id is None
        err = capsys.readouterr().err
        assert "plan_id" in err
        assert "p1.payee_id" in err


# ─── save_config ────────────────────────────────────────────────────────────


class TestSaveConfig:
    def test_creates_parent_dir(self, isolated_config_dir):
        assert not isolated_config_dir.exists()
        config.save_config(config.Config(plan_id="x"))
        assert isolated_config_dir.is_dir()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions only")
    def test_file_mode_is_0600(self, isolated_config_dir):
        config.save_config(config.Config(plan_id="x"))
        mode = stat.S_IMODE(config.config_path().stat().st_mode)
        assert mode == 0o600

    def test_omits_plan_subtables_when_empty(self, isolated_config_dir):
        config.save_config(config.Config())
        contents = config.config_path().read_text()
        assert "[" not in contents  # no subtables
        assert "schema_version" in contents

    def test_drops_empty_plan_subtable(self, isolated_config_dir):
        config.save_config(
            config.Config(plan_id="p1", plans={"p1": config.PlanDefaults()})
        )
        contents = config.config_path().read_text()
        assert "[p1]" not in contents


# ─── set_config_value ───────────────────────────────────────────────────────


class TestSetConfigValue:
    def test_sets_plan_id(self, isolated_config_dir):
        config.set_config_value("plan_id", "abc")
        assert config.load_config().plan_id == "abc"

    def test_unset_plan_id_with_none(self, isolated_config_dir):
        config.set_config_value("plan_id", "abc")
        config.set_config_value("plan_id", None)
        assert config.load_config().plan_id is None

    def test_unset_plan_id_with_empty_string(self, isolated_config_dir):
        config.set_config_value("plan_id", "abc")
        config.set_config_value("plan_id", "")
        assert config.load_config().plan_id is None

    def test_rejects_unknown_key(self, isolated_config_dir):
        with pytest.raises(ValueError, match="Unknown config key"):
            config.set_config_value("bogus", "x")

    def test_plan_scoped_requires_plan_id(self, isolated_config_dir):
        with pytest.raises(ValueError, match="requires a plan_id"):
            config.set_config_value("payee_id", "p-1")

    def test_plan_scoped_writes_per_plan(self, isolated_config_dir):
        config.set_config_value("payee_id", "pay-A", plan_id="plan-A")
        config.set_config_value("payee_id", "pay-B", plan_id="plan-B")
        cfg = config.load_config()
        assert cfg.defaults_for("plan-A").payee_id == "pay-A"
        assert cfg.defaults_for("plan-B").payee_id == "pay-B"

    def test_unsetting_last_field_drops_subtable(self, isolated_config_dir):
        config.set_config_value("payee_id", "pay-A", plan_id="plan-A")
        config.set_config_value("payee_id", None, plan_id="plan-A")
        cfg = config.load_config()
        assert "plan-A" not in cfg.plans


# ─── Keyring ────────────────────────────────────────────────────────────────


class TestKeyring:
    def test_get_returns_none_when_empty(self, stub_keyring):
        assert config.get_token_from_keyring() is None

    def test_set_then_get(self, stub_keyring):
        config.set_token_in_keyring("secret-token")
        assert config.get_token_from_keyring() == "secret-token"

    def test_delete_returns_true_when_present(self, stub_keyring):
        config.set_token_in_keyring("secret-token")
        assert config.delete_token_from_keyring() is True
        assert config.get_token_from_keyring() is None

    def test_delete_returns_false_when_absent(self, stub_keyring):
        assert config.delete_token_from_keyring() is False

    def test_get_returns_none_when_backend_fails(self, failing_keyring):
        assert config.get_token_from_keyring() is None

    def test_set_raises_when_backend_fails(self, failing_keyring):
        with pytest.raises(config.KeyringUnavailable):
            config.set_token_in_keyring("x")

    def test_delete_returns_false_when_backend_fails(self, failing_keyring):
        assert config.delete_token_from_keyring() is False


# ─── Precedence resolution ──────────────────────────────────────────────────


class TestResolve:
    def test_resolve_plan_id_cli_wins(self):
        cfg = config.Config(plan_id="from-config")
        assert config.resolve_plan_id("from-cli", cfg) == "from-cli"

    def test_resolve_plan_id_falls_back_to_config(self):
        cfg = config.Config(plan_id="from-config")
        assert config.resolve_plan_id(None, cfg) == "from-config"

    def test_resolve_plan_id_none_when_unset(self):
        assert config.resolve_plan_id(None, config.Config()) is None

    def test_resolve_payee_id_per_plan(self):
        cfg = config.Config(
            plans={"p1": config.PlanDefaults(payee_id="from-config")}
        )
        assert config.resolve_payee_id(None, cfg, "p1") == "from-config"
        assert config.resolve_payee_id("from-cli", cfg, "p1") == "from-cli"
        assert config.resolve_payee_id(None, cfg, "other-plan") is None
        assert config.resolve_payee_id(None, cfg, None) is None

    def test_resolve_category_group_id_per_plan(self):
        cfg = config.Config(
            plans={"p1": config.PlanDefaults(category_group_id="from-config")}
        )
        assert config.resolve_category_group_id(None, cfg, "p1") == "from-config"
        assert config.resolve_category_group_id("from-cli", cfg, "p1") == "from-cli"
        assert config.resolve_category_group_id(None, cfg, "other-plan") is None

    def test_resolve_token_cli_wins(self, stub_keyring):
        config.set_token_in_keyring("from-keyring")
        assert config.resolve_token("from-cli") == "from-cli"

    def test_resolve_token_falls_back_to_keyring(self, stub_keyring):
        config.set_token_in_keyring("from-keyring")
        assert config.resolve_token() == "from-keyring"

    def test_resolve_token_none_when_nothing_set(self, stub_keyring):
        assert config.resolve_token() is None
