import pytest

from documents_editor import config
from documents_editor.config import AgentSettings, ConfigError, agent_settings, substitute


def test_agent_settings_and_substitution(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[agent.simple]\ncommand = ["agent", "{requests}"]\ntimeout_minutes = 5\n')
    s = agent_settings("simple", ["default"], path=cfg)
    assert s == AgentSettings(["agent", "{requests}"], 5)
    assert substitute(s.command, {"requests": "/p/Астро проект/edit_requests.json"}) == [
        "agent", "/p/Астро проект/edit_requests.json"]
    with pytest.raises(ConfigError, match="missing"):
        substitute(["{missing}"], {})


def test_defaults_for_missing_file_and_section(tmp_path):
    assert agent_settings("x", ["d"], path=tmp_path / "none.toml") == AgentSettings(["d"], 30)
    cfg = tmp_path / "c.toml"
    cfg.write_text('[agent.other]\ncommand = ["a"]\n')
    assert agent_settings("x", ["d"], path=cfg) == AgentSettings(["d"], 30)


def test_invalid_settings_raise(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[agent.x]\ncommand = "oops"\n')
    with pytest.raises(ConfigError):
        agent_settings("x", ["d"], path=cfg)


def test_confirmation_roundtrip(tmp_path):
    p = tmp_path / "sub" / "confirmed.json"
    assert not config.is_confirmed(["a", "b"], path=p)
    config.confirm(["a", "b"], path=p)
    config.confirm(["a", "b"], path=p)
    assert config.is_confirmed(["a", "b"], path=p)
    assert not config.is_confirmed(["a"], path=p)


def test_config_path_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_path() == tmp_path / "documents-editor" / "documents-editor.toml"


def test_agent_that_is_not_a_table_raises(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('agent = "x"\n')
    with pytest.raises(ConfigError, match="table"):
        agent_settings("simple", ["d"], path=cfg)
    cfg.write_text('[agent]\nsimple = "x"\n')
    with pytest.raises(ConfigError, match="table"):
        agent_settings("simple", ["d"], path=cfg)


def test_non_utf8_config_raises_config_error(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_bytes(b'[agent.simple]\ncommand = ["\xff"]\n')
    with pytest.raises(ConfigError, match="cannot read"):
        agent_settings("simple", ["d"], path=cfg)
