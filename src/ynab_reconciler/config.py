"""Configuration storage for ynab-reconciler.

The token lives in the OS keychain (macOS Keychain / Secret Service /
Windows Credential Manager) via the `keyring` library. Non-secret
defaults live in a TOML file under `$XDG_CONFIG_HOME/ynab-reconciler/`
(or `~/.config/ynab-reconciler/` if XDG is unset).

Precedence for any value:

    CLI flag → config file → keyring (token only) → interactive prompt

Nothing else in the codebase should import `keyring`, `tomllib`,
`tomli_w`, or `platformdirs` — this module is the single boundary.
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional

import keyring
import keyring.errors
import platformdirs
import tomli_w

APP_NAME = "ynab-reconciler"
KEYRING_SERVICE = "ynab-reconciler"
KEYRING_USERNAME = "default"
CONFIG_KEYS = ("plan_id", "payee_id", "category_group_id")
SCHEMA_VERSION = 1


class KeyringUnavailable(Exception):
    """Raised when writing to the OS keychain fails."""


@dataclass
class Config:
    plan_id: Optional[str] = None
    payee_id: Optional[str] = None
    category_group_id: Optional[str] = None


# ─── Paths ──────────────────────────────────────────────────────────────────


def config_dir() -> Path:
    """Resolve the config directory.

    Honours `$XDG_CONFIG_HOME` on every platform; falls back to
    `~/.config/<APP_NAME>` on macOS and Linux for consistency, and to
    platformdirs' native default on Windows.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    if sys.platform == "win32":
        return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=False))
    return Path.home() / ".config" / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.toml"


# ─── File I/O ───────────────────────────────────────────────────────────────


def load_config() -> Config:
    """Read the TOML config. Returns an empty Config if the file is missing
    or malformed (with a warning on stderr for the malformed case)."""
    path = config_path()
    if not path.exists():
        return Config()

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"warning: ignoring malformed config at {path}: {e}", file=sys.stderr)
        return Config()

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        print(f"warning: [defaults] in {path} is not a table; ignoring", file=sys.stderr)
        defaults = {}

    cfg = Config()
    for field in fields(Config):
        value = defaults.get(field.name)
        if value is None:
            continue
        if not isinstance(value, str):
            print(
                f"warning: ignoring non-string value for defaults.{field.name} in {path}",
                file=sys.stderr,
            )
            continue
        setattr(cfg, field.name, value or None)

    unknown = set(defaults) - set(CONFIG_KEYS)
    for key in sorted(unknown):
        print(f"warning: ignoring unknown key 'defaults.{key}' in {path}", file=sys.stderr)

    return cfg


def save_config(cfg: Config) -> None:
    """Atomically write config to disk. Creates the directory with mode 0700
    and the file with mode 0600."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    defaults = {k: v for k in CONFIG_KEYS if (v := getattr(cfg, k)) is not None}
    doc: dict = {"schema_version": SCHEMA_VERSION}
    if defaults:
        doc["defaults"] = defaults

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(tomli_w.dumps(doc).encode("utf-8"))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def set_config_value(key: str, value: Optional[str]) -> None:
    """Set or unset a single config key. Pass None or an empty string to unset."""
    if key not in CONFIG_KEYS:
        raise ValueError(
            f"Unknown config key: {key!r}. Valid keys: {', '.join(CONFIG_KEYS)}"
        )
    cfg = load_config()
    setattr(cfg, key, value or None)
    save_config(cfg)


# ─── Keyring ────────────────────────────────────────────────────────────────


def get_token_from_keyring() -> Optional[str]:
    """Return the stored token, or None if absent or the keyring is unavailable."""
    try:
        token = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring.errors.KeyringError:
        return None
    return token or None


def set_token_in_keyring(token: str) -> None:
    """Store the token in the OS keychain.

    Raises KeyringUnavailable if the backend can't be reached.
    """
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)
    except keyring.errors.KeyringError as e:
        raise KeyringUnavailable(str(e)) from e


def delete_token_from_keyring() -> bool:
    """Delete the stored token. Returns True if something was deleted,
    False if there was nothing to delete (or the backend is unavailable)."""
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        return True
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.KeyringError:
        return False


_BACKEND_LABELS = {
    "keyring.backends.macOS": "macOS Keychain",
    "keyring.backends.kwallet": "KWallet",
    "keyring.backends.SecretService": "Secret Service",
    "keyring.backends.Windows": "Windows Credential Manager",
    "keyring.backends.fail": "unavailable",
    "keyring.backends.null": "unavailable",
}


def keyring_backend_name() -> str:
    """Human-readable name of the active keyring backend, for `auth status`."""
    try:
        backend = keyring.get_keyring()
    except keyring.errors.KeyringError as e:
        return f"unavailable ({e})"
    module = type(backend).__module__
    return _BACKEND_LABELS.get(module, f"{module}.{type(backend).__name__}")


# ─── Precedence resolution ──────────────────────────────────────────────────


def resolve_token(cli_value: Optional[str] = None) -> Optional[str]:
    """CLI flag > keyring. Returns None when neither has a value."""
    if cli_value:
        return cli_value
    return get_token_from_keyring()


def _resolve(cli_value: Optional[str], cfg: Config, attr: str) -> Optional[str]:
    if cli_value:
        return cli_value
    return getattr(cfg, attr)


def resolve_plan_id(cli_value: Optional[str], cfg: Config) -> Optional[str]:
    return _resolve(cli_value, cfg, "plan_id")


def resolve_payee_id(cli_value: Optional[str], cfg: Config) -> Optional[str]:
    return _resolve(cli_value, cfg, "payee_id")


def resolve_category_group_id(cli_value: Optional[str], cfg: Config) -> Optional[str]:
    return _resolve(cli_value, cfg, "category_group_id")
