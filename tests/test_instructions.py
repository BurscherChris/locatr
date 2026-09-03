import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.instructions import find_agents_md, load_agents_md
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.skills import available_skills, load_skill, load_skills, relevant_skills_for_repository
from app.main import app


# ---------------------------------------------------------------------------
# System prompt tests
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_system_prompt_is_present(self):
        assert SYSTEM_PROMPT is not None
        assert len(SYSTEM_PROMPT) > 200

    def test_system_prompt_contains_core_rules(self):
        assert "Never work directly on main/master" in SYSTEM_PROMPT
        assert "Never commit secrets" in SYSTEM_PROMPT
        assert "Workflow" in SYSTEM_PROMPT

    def test_system_prompt_does_not_contain_repo_specifics(self):
        assert "lims" not in SYSTEM_PROMPT.lower()
        assert "locatr" not in SYSTEM_PROMPT.lower()

    def test_system_prompt_has_no_secrets(self):
        import re
        # Match actual credential patterns, not words containing "sk-"
        patterns = [re.compile(r'sk-[A-Za-z0-9]{10,}'), re.compile(r'lin-api-[A-Za-z0-9]+'),
                     re.compile(r'ghp_[A-Za-z0-9]{10,}'), re.compile(r'gho_[A-Za-z0-9]{10,}'),
                     re.compile(r'NEURON_API_KEY'), re.compile(r'LINEAR_API_KEY'), re.compile(r'GITHUB_TOKEN')]
        for p in patterns:
            match = p.search(SYSTEM_PROMPT)
            assert not match, f"credential pattern matched in system prompt: {match.group() if match else 'unknown'}"


# ---------------------------------------------------------------------------
# Skills tests
# ---------------------------------------------------------------------------

class TestSkills:
    def test_available_skills_returns_list(self):
        skills = available_skills()
        assert "core" in skills
        assert "testing" in skills
        assert "git" in skills

    def test_load_skill_returns_content(self):
        content = load_skill("core")
        assert content is not None
        assert len(content) > 50
        assert "IMPLEMENT" in content

    def test_load_missing_skill_returns_none(self):
        assert load_skill("nonexistent_skill_xyz") is None

    def test_load_skills_multiple(self):
        result = load_skills(["core", "testing"])
        assert "core" in result
        assert "testing" in result

    def test_load_skills_missing_does_not_crash(self):
        result = load_skills(["core", "nonexistent_skill_xyz"])
        assert "core" in result
        assert "nonexistent_skill_xyz" not in result

    def test_relevant_skills_includes_python(self):
        skills = relevant_skills_for_repository("my-python-project", "implement feature")
        assert "python" in skills

    def test_relevant_skills_includes_react(self):
        skills = relevant_skills_for_repository("my-react-app", "add component")
        assert "react" in skills

    def test_relevant_skills_includes_database(self):
        skills = relevant_skills_for_repository("my-app", "add database migration")
        assert "database" in skills

    def test_relevant_skills_includes_api(self):
        skills = relevant_skills_for_repository("my-api", "implement endpoint")
        assert "api" in skills

    def test_relevant_skills_always_has_core_and_testing(self):
        for name in ["my-project", "frontend", "backend"]:
            skills = relevant_skills_for_repository(name, "task")
            assert "core" in skills, f"core missing for {name}"
            assert "testing" in skills, f"testing missing for {name}"
            assert "git" in skills, f"git missing for {name}"
            assert "github" in skills, f"github missing for {name}"


# ---------------------------------------------------------------------------
# AGENTS.md discovery tests
# ---------------------------------------------------------------------------

class TestAgentsMdDiscovery:
    def test_find_no_agents_md(self, tmp_path):
        assert find_agents_md(tmp_path) is None

    def test_find_agents_md_in_workspace(self, tmp_path):
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# Test")
        assert find_agents_md(tmp_path) == agents

    def test_find_agents_md_in_repository_subdir(self, tmp_path):
        repo_dir = tmp_path / "repository"
        repo_dir.mkdir()
        agents = repo_dir / "AGENTS.md"
        agents.write_text("# Test")
        assert find_agents_md(tmp_path) == agents

    def test_load_agents_md_when_present(self, tmp_path):
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# My custom instructions\n- rule1\n- rule2")
        content = load_agents_md(tmp_path, "TEST-1")
        assert "rule1" in content
        assert "rule2" in content

    def test_load_agents_md_when_absent(self, tmp_path):
        content = load_agents_md(tmp_path, "TEST-2")
        assert content == ""


# ---------------------------------------------------------------------------
# Context builder smoke test (no real workspace)
# ---------------------------------------------------------------------------

class TestContextBuilder:
    def test_context_includes_system_prompt(self):
        from app.agent.runner import build_context
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            context = build_context("my task", "TEST-1", "https://github.com/org/repo.git", Path(d), "main", None)
            # The system prompt is not in the context — it's a separate message. The context has Repository/Skills/Task.
            assert "Repository" in context
            assert "TEST-1" in context
            assert "my task" in context

    def test_context_includes_agents_md(self, tmp_path):
        from app.agent.runner import build_context
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# Custom instructions\nfollow the pattern")
        context = build_context("my task", "TEST-2", "https://github.com/org/repo.git", tmp_path, "main", None)
        assert "Custom instructions" in context
        assert "follow the pattern" in context

    def test_context_includes_skills(self):
        from app.agent.runner import build_context
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            context = build_context("implement python endpoint", "TEST-3", "my-python-api", Path(d), "main", None)
            assert "python" in context.lower() or "WORKFLOW" in context


# ---------------------------------------------------------------------------
# Secret leak check across instruction files
# ---------------------------------------------------------------------------

class TestInstructionSecrets:
    def test_no_secrets_in_skill_files(self):
        import re
        skills_dir = Path(__file__).resolve().parent.parent / "skills"
        # Match actual credential patterns, not words containing "sk-"
        patterns = [re.compile(r'sk-[A-Za-z0-9]{10,}'), re.compile(r'lin-api-[A-Za-z0-9]+'),
                     re.compile(r'ghp_[A-Za-z0-9]{10,}'), re.compile(r'gho_[A-Za-z0-9]{10,}'),
                     re.compile(r'NEURON_API_KEY'), re.compile(r'LINEAR_API_KEY'), re.compile(r'GITHUB_TOKEN')]
        for f in skills_dir.glob("*.md"):
            text = f.read_text()
            for p in patterns:
                match = p.search(text)
                assert not match, f"credential pattern matched in {f.name}: {match.group()}"

    def test_no_secrets_in_example_agents(self):
        examples_dir = Path(__file__).resolve().parent.parent / "examples"
        secrets = ["sk-", "lin-api-", "ghp_", "NEURON_API_KEY"]
        for f in examples_dir.glob("*.md"):
            text = f.read_text()
            for s in secrets:
                assert s not in text, f"secret pattern found in {f.name}: {s}"