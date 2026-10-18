"""Credential construction for the supported authentication modes.

* ``ntlm`` / ``basic``  - on-premises Exchange Server (username + password).
* ``oauth_app``          - Exchange Online, app-only client-credentials flow with
                           impersonation (needs ``full_access_as_app`` consent).
* ``oauth_delegated``    - Exchange Online, signed-in user via MSAL device-code
                           flow. Run ``exchange-mcp-server login`` once to cache a
                           refresh token; the server then refreshes silently.

Note: Microsoft is retiring EWS in Exchange Online (phased blocking from
2026-10-01, permanent shutdown 2027-04-01). On-premises Exchange Server is not
affected. The OAuth modes only work for tenants that still allow EWS.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from exchangelib import NTLM, Configuration, Credentials, Identity
from exchangelib.credentials import OAuth2AuthorizationCodeCredentials, OAuth2Credentials
from exchangelib.transport import BASIC, OAUTH2

from .config import ConfigError, Settings

EWS_SCOPE = ["https://outlook.office365.com/EWS.AccessAsUser.All"]


def _require_msal():
    try:
        import msal
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ConfigError(
            "oauth_delegated needs the 'msal' package. Install with: pip install 'exchange-mcp-server[oauth]'"
        ) from exc
    return msal


class MsalTokenStore:
    """Thin wrapper around an MSAL public client with a file-backed token cache."""

    def __init__(self, settings: Settings):
        msal = _require_msal()
        self.path = Path(settings.token_cache)
        self.username = settings.email
        self.cache = msal.SerializableTokenCache()
        if self.path.is_file():
            self.cache.deserialize(self.path.read_text(encoding="utf-8"))
        authority = f"https://login.microsoftonline.com/{settings.tenant_id or 'organizations'}"
        self.app = msal.PublicClientApplication(settings.client_id, authority=authority, token_cache=self.cache)

    def _persist(self) -> None:
        if self.cache.has_state_changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.cache.serialize(), encoding="utf-8")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def acquire_silent(self) -> dict[str, Any] | None:
        accounts = self.app.get_accounts(username=self.username) or self.app.get_accounts()
        if not accounts:
            return None
        result = self.app.acquire_token_silent(EWS_SCOPE, account=accounts[0])
        self._persist()
        if result and "access_token" in result:
            return result
        return None

    def login_device_code(self) -> dict[str, Any]:
        flow = self.app.initiate_device_flow(scopes=EWS_SCOPE)
        if "user_code" not in flow:
            raise ConfigError(f"Could not start device-code flow: {json.dumps(flow, ensure_ascii=False)}")
        # stderr: stdout is reserved for the MCP protocol.
        print(flow["message"], file=sys.stderr, flush=True)
        result = self.app.acquire_token_by_device_flow(flow)
        self._persist()
        if "access_token" not in result:
            raise ConfigError(f"Sign-in failed: {result.get('error_description') or result}")
        return result


def _to_oauth_token(result: dict[str, Any]) -> dict[str, Any]:
    expires_in = int(result.get("expires_in", 3600))
    return {
        "access_token": result["access_token"],
        "token_type": "Bearer",
        "expires_in": expires_in,
        "expires_at": time.time() + expires_in - 60,
    }


class MsalDelegatedCredentials(OAuth2AuthorizationCodeCredentials):
    """Delegated OAuth credentials whose access token is refreshed through MSAL."""

    def __init__(self, store: MsalTokenStore):
        result = store.acquire_silent()
        if result is None:
            raise ConfigError(
                "No cached Exchange Online sign-in found. Run `exchange-mcp-server login` once in a terminal."
            )
        super().__init__(client_id=store.app.client_id, access_token=_to_oauth_token(result))
        self._store = store

    def refresh(self, session):  # called by exchangelib when the token expires
        result = self._store.acquire_silent()
        if result is None:
            raise ConfigError("Exchange Online token expired and could not be refreshed; run `exchange-mcp-server login`.")
        self.access_token = _to_oauth_token(result)


def build_configuration(settings: Settings) -> tuple[Configuration | None, Any, str]:
    """Return ``(configuration, credentials, access_type)``.

    ``configuration`` is ``None`` when autodiscover is used; callers then pass the
    credentials to ``Account(autodiscover=True)``.
    """
    from exchangelib import DELEGATE, IMPERSONATION

    access_type = DELEGATE
    if settings.auth == "ntlm":
        credentials: Any = Credentials(username=settings.login_name, password=settings.password)
        auth_type = NTLM
    elif settings.auth == "basic":
        credentials = Credentials(username=settings.login_name, password=settings.password)
        auth_type = BASIC
    elif settings.auth == "oauth_app":
        credentials = OAuth2Credentials(
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            tenant_id=settings.tenant_id,
            identity=Identity(primary_smtp_address=settings.email),
        )
        auth_type = OAUTH2
        access_type = IMPERSONATION
    elif settings.auth == "oauth_delegated":
        credentials = MsalDelegatedCredentials(MsalTokenStore(settings))
        auth_type = OAUTH2
    else:  # validated earlier
        raise ConfigError(f"Unsupported auth mode {settings.auth!r}")

    if settings.autodiscover:
        return None, credentials, access_type
    config = Configuration(service_endpoint=settings.server, credentials=credentials, auth_type=auth_type)
    return config, credentials, access_type
