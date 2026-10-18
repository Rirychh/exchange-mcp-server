from pathlib import Path

import pytest

from exchange_mcp_server import config as cfg
from exchange_mcp_server.config import ConfigError, Settings, parse_env_file, settings_from_env


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "home").mkdir()
    return tmp_path


def test_parse_env_file_handles_quotes_comments_export(tmp_path):
    f = tmp_path / "x.env"
    f.write_text('# c\nexport A=1\nB="two words"\nC=\'3\'\n\nbad line\nD = spaced \n', encoding="utf-8")
    assert parse_env_file(f) == {"A": "1", "B": "two words", "C": "3", "D": "spaced"}


def test_environment_wins_over_env_file(tmp_path):
    (tmp_path / ".env").write_text("EXCHANGE_EMAIL=file@example.com\nEXCHANGE_SERVER=https://f/EWS\n")
    s = settings_from_env({"EXCHANGE_EMAIL": "env@example.com"})
    assert s.email == "env@example.com"
    assert s.server == "https://f/EWS"
    assert s.env_file == tmp_path / ".env"


def test_explicit_env_file_takes_priority(tmp_path):
    (tmp_path / ".env").write_text("EXCHANGE_EMAIL=cwd@example.com\n")
    other = tmp_path / "other.env"
    other.write_text("EXCHANGE_EMAIL=explicit@example.com\n")
    s = settings_from_env({"EXCHANGE_MCP_ENV_FILE": str(other)})
    assert s.email == "explicit@example.com"


def test_legacy_home_file_is_read(isolated_home):
    (isolated_home / "home" / "exchange_mcp.env").write_text("EXCHANGE_EMAIL=legacy@example.com\n")
    assert settings_from_env({}).email == "legacy@example.com"


def test_oauth_defaults_to_exchange_online_endpoint():
    s = settings_from_env({"EXCHANGE_AUTH": "oauth_delegated", "EXCHANGE_EMAIL": "a@b.c"}, load_file=False)
    assert s.server == cfg.EXCHANGE_ONLINE_EWS


def test_lists_and_flags():
    s = settings_from_env({
        "EXCHANGE_EMAIL": "a@b.c", "EXCHANGE_SERVER": "https://x", "EXCHANGE_PASSWORD": "p",
        "EXCHANGE_READ_ONLY": "yes", "EXCHANGE_ALLOW_SEND": "false", "EXCHANGE_ATTACHMENT_DIRS": "/tmp/a, /tmp/b;/tmp/c",
    }, load_file=False)
    assert s.read_only and not s.allow_send
    assert [p.name for p in s.attachment_dirs] == ["a", "b", "c"]


@pytest.mark.parametrize("kwargs, message", [
    ({"email": ""}, "EXCHANGE_EMAIL"),
    ({"auth": "kerberos"}, "EXCHANGE_AUTH"),
    ({"server": ""}, "EXCHANGE_SERVER"),
    ({"password": ""}, "EXCHANGE_PASSWORD"),
    ({"auth": "oauth_app"}, "EXCHANGE_TENANT_ID"),
    ({"auth": "oauth_delegated"}, "EXCHANGE_CLIENT_ID"),
    ({"room_source": "x"}, "EXCHANGE_ROOM_SOURCE"),
])
def test_validate_errors(kwargs, message):
    base = {"email": "a@b.c", "server": "https://x", "password": "p"}
    with pytest.raises(ConfigError, match=message):
        Settings(**{**base, **kwargs}).validate()


def test_login_name():
    assert Settings(email="jane@corp.com", domain="CORP").login_name == "CORP\\jane"
    assert Settings(email="jane@corp.com", domain="CORP", auth="basic").login_name == "jane@corp.com"
    assert Settings(email="jane@corp.com", username="CORP\\jdoe", domain="X").login_name == "CORP\\jdoe"


def test_password_not_in_repr():
    assert "s3cret" not in repr(Settings(email="a@b.c", password="s3cret", client_secret="s3cret"))
