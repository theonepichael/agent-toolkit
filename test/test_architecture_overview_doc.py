from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_readme_harness_tiers_not_stale():
    readme = (REPO_ROOT / "README.md").read_text()
    # Ensure no best-effort label attached to Codex CLI or Copilot/AGY in harness tiers
    assert "Codex CLI (OpenAI) — best-effort tier" not in readme
    assert "best-effort tier, like Copilot/AGY" not in readme
    assert "docs/architecture/overview.md" in readme


def test_agents_md_links_overview_doc():
    agents = (REPO_ROOT / "AGENTS.md").read_text()
    assert "docs/architecture/overview.md" in agents


def test_overview_architecture_doc_exists_and_covers_lifecycles():
    overview_path = REPO_ROOT / "docs" / "architecture" / "overview.md"
    assert overview_path.exists(), "docs/architecture/overview.md must exist"

    content = overview_path.read_text()
    # Check mermaid diagrams
    assert "```mermaid" in content
    # Check 4 key lifecycle flows
    assert "backlog-item" in content
    assert "Guard Rails" in content or "guard rails" in content
    assert "Settings Drift" in content or "settings drift" in content
    assert "Code Generation" in content or "code generation" in content

    # Check that referenced repository files in markdown links exist
    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for text, link in link_pattern.findall(content):
        # ignore external links or section anchors
        if link.startswith("http") or link.startswith("#") or link.startswith("mailto:"):
            continue
        # handle file path (strip anchor if present)
        target_path_str = link.split("#")[0]
        if not target_path_str:
            continue
        target = (overview_path.parent / target_path_str).resolve()
        assert target.exists(), f"Broken link in overview.md: {link} -> {target}"
