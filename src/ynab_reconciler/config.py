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
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import keyring
import keyring.errors
import platformdirs
import tomli_w

APP_NAME = "ynab-reconciler"
KEYRING_SERVICE = "ynab-reconciler"
KEYRING_USERNAME = "default"
PLAN_SCOPED_KEYS = ("payee_id", "category_group_id")
CONFIG_KEYS = ("plan_id", *PLAN_SCOPED_KEYS)
RESERVED_TOP_LEVEL_KEYS = ("schema_version", "plan_id", "connections", "pairings")
SCHEMA_VERSION = 3


class KeyringUnavailable(Exception):
    """Raised when writing to the OS keychain fails."""


@dataclass
class PlanDefaults:
    payee_id: Optional[str] = None
    category_group_id: Optional[str] = None

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))


@dataclass
class Connection:
    """A configured remote balance source (e.g. an Interactive Brokers Flex query).

    Non-secret config only — the access token lives in the OS keychain, keyed by
    the connection's name (see get/set/delete_connection_secret).
    """

    type: str
    query_id: str


@dataclass
class Pairing:
    """Links a YNAB account to a connection's reported balance.

    `account_id` (a globally-unique YNAB UUID) is the dict key in Config.pairings,
    so it isn't stored here. `ib_account_id` selects which account within the
    connection's report to read; `field` selects which figure (see the
    connections registry for valid values).
    """

    connection: str
    ib_account_id: str
    field: str = "net_liquidation"


@dataclass
class Config:
    plan_id: Optional[str] = None
    plans: dict[str, PlanDefaults] = field(default_factory=dict)
    connections: dict[str, Connection] = field(default_factory=dict)
    pairings: dict[str, Pairing] = field(default_factory=dict)

    def defaults_for(self, plan_id: Optional[str]) -> PlanDefaults:
        if plan_id is None:
            return PlanDefaults()
        return self.plans.get(plan_id, PlanDefaults())


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

    cfg = Config()

    plan_id = data.get("plan_id")
    if plan_id is not None:
        if isinstance(plan_id, str):
            cfg.plan_id = plan_id or None
        else:
            print(
                f"warning: ignoring non-string value for plan_id in {path}",
                file=sys.stderr,
            )

    for key, value in data.items():
        if key in RESERVED_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            print(f"warning: ignoring unknown key {key!r} in {path}", file=sys.stderr)
            continue
        cfg.plans[key] = _parse_plan_defaults(key, value, path)

    cfg.connections = _parse_connections(data.get("connections"), path)
    cfg.pairings = _parse_pairings(data.get("pairings"), path)

    return cfg


def _parse_plan_defaults(plan_id: str, table: dict, path: Path) -> PlanDefaults:
    pd = PlanDefaults()
    for sub_key, sub_value in table.items():
        if sub_key not in PLAN_SCOPED_KEYS:
            print(
                f"warning: ignoring unknown key '{plan_id}.{sub_key}' in {path}",
                file=sys.stderr,
            )
            continue
        if not isinstance(sub_value, str):
            print(
                f"warning: ignoring non-string value for '{plan_id}.{sub_key}' in {path}",
                file=sys.stderr,
            )
            continue
        setattr(pd, sub_key, sub_value or None)
    return pd


def _parse_connections(raw, path: Path) -> dict[str, Connection]:
    out: dict[str, Connection] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        print(f"warning: ignoring non-table 'connections' in {path}", file=sys.stderr)
        return out
    for name, table in raw.items():
        if not isinstance(table, dict):
            print(
                f"warning: ignoring malformed connection '{name}' in {path}",
                file=sys.stderr,
            )
            continue
        ctype = table.get("type")
        query_id = table.get("query_id")
        if not isinstance(ctype, str) or not isinstance(query_id, str):
            print(
                f"warning: ignoring connection '{name}' (missing type/query_id) in {path}",
                file=sys.stderr,
            )
            continue
        out[name] = Connection(type=ctype, query_id=query_id)
    return out


def _parse_pairings(raw, path: Path) -> dict[str, Pairing]:
    out: dict[str, Pairing] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        print(f"warning: ignoring non-table 'pairings' in {path}", file=sys.stderr)
        return out
    for account_id, table in raw.items():
        if not isinstance(table, dict):
            print(
                f"warning: ignoring malformed pairing '{account_id}' in {path}",
                file=sys.stderr,
            )
            continue
        connection = table.get("connection")
        ib_account_id = table.get("ib_account_id")
        if not isinstance(connection, str) or not isinstance(ib_account_id, str):
            print(
                f"warning: ignoring pairing '{account_id}' (missing connection/"
                f"ib_account_id) in {path}",
                file=sys.stderr,
            )
            continue
        field_value = table.get("field")
        field_value = field_value if isinstance(field_value, str) and field_value else "net_liquidation"
        out[account_id] = Pairing(
            connection=connection,
            ib_account_id=ib_account_id,
            field=field_value,
        )
    return out


def save_config(cfg: Config) -> None:
    """Atomically write config to disk. Creates the directory with mode 0700
    and the file with mode 0600."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    doc: dict = {"schema_version": SCHEMA_VERSION}
    if cfg.plan_id is not None:
        doc["plan_id"] = cfg.plan_id
    for pid in sorted(cfg.plans):
        pd = cfg.plans[pid]
        if pd.is_empty():
            continue
        doc[pid] = {k: getattr(pd, k) for k in PLAN_SCOPED_KEYS if getattr(pd, k) is not None}

    if cfg.connections:
        doc["connections"] = {
            name: {"type": c.type, "query_id": c.query_id}
            for name, c in sorted(cfg.connections.items())
        }
    if cfg.pairings:
        doc["pairings"] = {
            account_id: {
                "connection": p.connection,
                "ib_account_id": p.ib_account_id,
                "field": p.field,
            }
            for account_id, p in sorted(cfg.pairings.items())
        }

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(tomli_w.dumps(doc).encode("utf-8"))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def set_config_value(
    key: str, value: Optional[str], plan_id: Optional[str] = None
) -> None:
    """Set or unset a single config key. Pass None or an empty string to unset.

    `plan_id` is required for plan-scoped keys (payee_id, category_group_id)
    and ignored for top-level keys (plan_id).
    """
    if key not in CONFIG_KEYS:
        raise ValueError(
            f"Unknown config key: {key!r}. Valid keys: {', '.join(CONFIG_KEYS)}"
        )
    cfg = load_config()
    normalised = value or None

    if key == "plan_id":
        cfg.plan_id = normalised
    else:
        if not plan_id:
            raise ValueError(f"Setting {key!r} requires a plan_id.")
        pd = cfg.plans.get(plan_id, PlanDefaults())
        setattr(pd, key, normalised)
        if pd.is_empty():
            cfg.plans.pop(plan_id, None)
        else:
            cfg.plans[plan_id] = pd

    save_config(cfg)


# ─── Connections & pairings ─────────────────────────────────────────────────


def list_connections() -> dict[str, Connection]:
    """Return the configured connections, keyed by name."""
    return load_config().connections


def get_connection(name: str) -> Optional[Connection]:
    return load_config().connections.get(name)


def upsert_connection(name: str, connection: Connection) -> None:
    """Add or replace a connection's non-secret config."""
    cfg = load_config()
    cfg.connections[name] = connection
    save_config(cfg)


def remove_connection(name: str) -> bool:
    """Delete a connection, its stored secret, and any pairings that reference it.

    Returns True if a connection was removed."""
    cfg = load_config()
    if name not in cfg.connections:
        return False
    del cfg.connections[name]
    cfg.pairings = {
        acct: p for acct, p in cfg.pairings.items() if p.connection != name
    }
    save_config(cfg)
    delete_connection_secret(name)
    return True


def pairing_for(account_id: str) -> Optional[Pairing]:
    return load_config().pairings.get(account_id)


def set_pairing(account_id: str, pairing: Pairing) -> None:
    cfg = load_config()
    cfg.pairings[account_id] = pairing
    save_config(cfg)


def remove_pairing(account_id: str) -> bool:
    """Delete a pairing. Returns True if one was removed."""
    cfg = load_config()
    if account_id not in cfg.pairings:
        return False
    del cfg.pairings[account_id]
    save_config(cfg)
    return True


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


def _connection_username(name: str) -> str:
    """Keyring username under which a connection's secret is stored."""
    return f"connection:{name}"


def get_connection_secret(name: str) -> Optional[str]:
    """Return a connection's stored access token, or None if absent/unavailable."""
    try:
        secret = keyring.get_password(KEYRING_SERVICE, _connection_username(name))
    except keyring.errors.KeyringError:
        return None
    return secret or None


def set_connection_secret(name: str, secret: str) -> None:
    """Store a connection's access token in the OS keychain.

    Raises KeyringUnavailable if the backend can't be reached."""
    try:
        keyring.set_password(KEYRING_SERVICE, _connection_username(name), secret)
    except keyring.errors.KeyringError as e:
        raise KeyringUnavailable(str(e)) from e


def delete_connection_secret(name: str) -> bool:
    """Delete a connection's stored token. Returns True if something was deleted."""
    try:
        keyring.delete_password(KEYRING_SERVICE, _connection_username(name))
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


def resolve_plan_id(cli_value: Optional[str], cfg: Config) -> Optional[str]:
    if cli_value:
        return cli_value
    return cfg.plan_id


def resolve_payee_id(
    cli_value: Optional[str], cfg: Config, plan_id: Optional[str]
) -> Optional[str]:
    if cli_value:
        return cli_value
    return cfg.defaults_for(plan_id).payee_id


def resolve_category_group_id(
    cli_value: Optional[str], cfg: Config, plan_id: Optional[str]
) -> Optional[str]:
    if cli_value:
        return cli_value
    return cfg.defaults_for(plan_id).category_group_id
