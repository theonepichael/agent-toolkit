"""Tests for shell/agent-tools.zsh extraction and .zshrc decoupling."""

from pathlib import Path
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_agent_tools_linked_in_links_toml():
    links_data = tomllib.loads((REPO_ROOT / "links.toml").read_text())["link"]
    agent_tools_link = [
        link for link in links_data
        if link.get("src") == "shell/agent-tools.zsh"
    ]
    assert len(agent_tools_link) == 1, "shell/agent-tools.zsh must be in links.toml"
    assert agent_tools_link[0]["dest"] == "~/.agent-tools.zsh"


def test_agent_tools_file_exists_and_has_content():
    agent_tools = REPO_ROOT / "shell" / "agent-tools.zsh"
    assert agent_tools.is_file(), "shell/agent-tools.zsh must exist"
    content = agent_tools.read_text()
    assert ".zsh/completions" in content
    assert ".opencode/bin" in content
    assert ".copilot_aliases" in content


def test_zshrc_sources_agent_tools():
    zshrc = (REPO_ROOT / "zsh" / ".zshrc").read_text()
    assert "agent-tools.zsh" in zshrc
    assert ".copilot_aliases" not in zshrc
