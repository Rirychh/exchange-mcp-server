"""Configuration loading.

Settings come from environment variables. An optional ``.env``-style file is read
first and only fills in variables that are not already set in the environment.

Lookup order for the env file (first one that exists wins):

1. ``$EXCHANGE_MCP_ENV_FILE``
2. ``./.env`` (current working directory)
3. ``~/.config/exchange-mcp/.env``
4. ``~/exchange_mcp.env`` (legacy location)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

AUTH_MODES = ("ntlm", "basic", "oauth_app", "oauth_delegated")
ROOM_SOURCES = ("roomlists", "catalog", "both")

EXCHANGE_ONLINE_EWS = "https://outlook.office365.com/EWS/Exchange.asmx"


class ConfigError(ValueError):
    """Raised when the configuration is incomplete or invalid."""


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def candidate_env_files(environ: dict[str, str] | None = None) -> list[Path]:
    env = os.environ if environ is None else environ
    paths: list[Path] = []
    explicit = env.get("EXCHANGE_MCP_ENV_FILE")
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.append(Path.cwd() / ".env")
    paths.append(Path.home() / ".config" / "exchange-mcp" / ".env")
    paths.append(Path.home() / "exchange_mcp.env")
    return paths


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def load_env_file(environ: dict[str, str] | None = None) -> Path | None:
    """Merge the first env file found into ``environ`` (defaults to os.environ).

    Existing variables are never overwritten. Returns the path that was used.
    """
    target = os.environ if environ is None else environ
    for path in candidate_env_files(target):
        if path.is_file():
            for key, value in parse_env_file(path).items():
                target.setdefault(key, value)
            return path
    return None


def _split_list(value: str | None) -> list[str]:
    """Split a list given with ',' or ';' separators (':' is avoided because of Windows drive letters)."""
    if not value:
        return []
    return [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]


@dataclass
class Settings:
    email: str
    server: str = ""
    autodiscover: bool = False
    auth: str = "ntlm"
    username: str = ""
    password: str = field(default="", repr=False)
    domain: str = ""
    verify_ssl: bool = True

    # OAuth (Exchange Online)
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    token_cache: Path = field(default_factory=lambda: Path.home() / ".config" / "exchange-mcp" / "token_cache.json")

    # Behaviour
    timezone: str = ""  # IANA name; empty = server host local time zone
    read_only: bool = False
    allow_send: bool = True
    room_source: str = "both"
    rooms_file: Path = field(default_factory=lambda: Path.home() / ".config" / "exchange-mcp" / "rooms.json")
    attachment_dirs: list[Path] = field(default_factory=list)
    max_attachment_mb: int = 20
    signature_dir: Path | None = None
    env_file: Path | None = None

    def validate(self) -> None:
        if not self.email:
            raise ConfigError("EXCHANGE_EMAIL is required.")
        if self.auth not in AUTH_MODES:
            raise ConfigError(f"EXCHANGE_AUTH must be one of {', '.join(AUTH_MODES)} (got {self.auth!r}).")
        if self.room_source not in ROOM_SOURCES:
            raise ConfigError(
                f"EXCHANGE_ROOM_SOURCE must be one of {', '.join(ROOM_SOURCES)} (got {self.room_source!r})."
            )
        if not self.server and not self.autodiscover:
            raise ConfigError("Set EXCHANGE_SERVER to your EWS URL, or EXCHANGE_AUTODISCOVER=true.")
        if self.auth in ("ntlm", "basic") and not self.password:
            raise ConfigError("EXCHANGE_PASSWORD is required for ntlm/basic authentication.")
        if self.auth == "oauth_app":
            missing = [n for n, v in (("EXCHANGE_TENANT_ID", self.tenant_id), ("EXCHANGE_CLIENT_ID", self.client_id),
                                      ("EXCHANGE_CLIENT_SECRET", self.client_secret)) if not v]
            if missing:
                raise ConfigError("oauth_app authentication requires " + ", ".join(missing) + ".")
        if self.auth == "oauth_delegated" and not self.client_id:
            raise ConfigError("oauth_delegated authentication requires EXCHANGE_CLIENT_ID.")

    @property
    def login_name(self) -> str:
        """The user name sent to the server for ntlm/basic auth."""
        user = self.username or self.email
        if self.auth == "ntlm" and self.domain and "\\" not in user:
            return f"{self.domain}\\{user.split('@')[0]}"
        return user


def settings_from_env(environ: dict[str, str] | None = None, *, load_file: bool = True) -> Settings:
    env = dict(os.environ) if environ is None else dict(environ)
    env_file = load_env_file(env) if load_file else None

    def get(name: str, default: str = "") -> str:
        return env.get(name, default).strip()

    auth = get("EXCHANGE_AUTH", "ntlm").lower()
    server = get("EXCHANGE_SERVER") or get("EXCHANGE_URL")
    if not server and auth.startswith("oauth") and not _truthy(get("EXCHANGE_AUTODISCOVER")):
        server = EXCHANGE_ONLINE_EWS

    settings = Settings(
        email=get("EXCHANGE_EMAIL"),
        server=server,
        autodiscover=_truthy(get("EXCHANGE_AUTODISCOVER")),
        auth=auth,
        username=get("EXCHANGE_USERNAME"),
        password=env.get("EXCHANGE_PASSWORD", ""),
        domain=get("EXCHANGE_DOMAIN"),
        verify_ssl=_truthy(get("EXCHANGE_VERIFY_SSL"), default=True),
        tenant_id=get("EXCHANGE_TENANT_ID"),
        client_id=get("EXCHANGE_CLIENT_ID"),
        client_secret=env.get("EXCHANGE_CLIENT_SECRET", ""),
        timezone=get("EXCHANGE_TIMEZONE"),
        read_only=_truthy(get("EXCHANGE_READ_ONLY")),
        allow_send=_truthy(get("EXCHANGE_ALLOW_SEND"), default=True),
        room_source=get("EXCHANGE_ROOM_SOURCE", "both").lower(),
        attachment_dirs=[Path(p).expanduser().resolve() for p in _split_list(get("EXCHANGE_ATTACHMENT_DIRS"))],
        env_file=env_file,
    )
    if get("EXCHANGE_TOKEN_CACHE"):
        settings.token_cache = Path(get("EXCHANGE_TOKEN_CACHE")).expanduser()
    if get("EXCHANGE_ROOMS_FILE"):
        settings.rooms_file = Path(get("EXCHANGE_ROOMS_FILE")).expanduser()
    elif not settings.rooms_file.exists() and (Path.home() / "exchange_rooms.json").exists():
        settings.rooms_file = Path.home() / "exchange_rooms.json"  # legacy location
    if get("EXCHANGE_MAX_ATTACHMENT_MB"):
        try:
            settings.max_attachment_mb = int(get("EXCHANGE_MAX_ATTACHMENT_MB"))
        except ValueError as exc:
            raise ConfigError("EXCHANGE_MAX_ATTACHMENT_MB must be an integer.") from exc
    if get("EXCHANGE_SIGNATURE_DIR"):
        settings.signature_dir = Path(get("EXCHANGE_SIGNATURE_DIR")).expanduser()
    elif env.get("APPDATA"):
        settings.signature_dir = Path(env["APPDATA"]) / "Microsoft" / "Signatures"
    return settings
