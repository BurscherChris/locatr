import json
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.instructions import find_agents_md, load_agents_md
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.skills import (
    AGENT_SKILLS_DIR,
    AGENT_CORE_SKILLS,
    available_agent_skills,
    load_agent_skill,
    load_agent_skills,
    discover_repository_skills,
    load_repository_skills,
    detect_technologies,
)
from app.main import app


# ---------------------------------------------------------------------------
# System prompt tests
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_system_prompt_is_present(self):
        assert SYSTEM_PROMPT is not None
        assert len(SYSTEM_PROMPT) > 200

    def test_system_prompt_contains_core_rules(self):
        assert "Never commit secrets" in SYSTEM_PROMPT
        assert "Workflow" in SYSTEM_PROMPT

    def test_system_prompt_does_not_contain_repo_specifics(self):
        assert "lims" not in SYSTEM_PROMPT.lower()
        assert "locatr" not in SYSTEM_PROMPT.lower()
        assert "python" not in SYSTEM_PROMPT.lower()
        assert "react" not in SYSTEM_PROMPT.lower()
        assert "next" not in SYSTEM_PROMPT.lower()

    def test_system_prompt_has_no_secrets(self):
        import re
        patterns = [re.compile(r'sk-[A-Za-z0-9]{10,}'), re.compile(r'lin-api-[A-Za-z0-9]+'),
                     re.compile(r'ghp_[A-Za-z0-9]{10,}'), re.compile(r'gho_[A-Za-z0-9]{10,}'),
                     re.compile(r'NEURON_API_KEY'), re.compile(r'LINEAR_API_KEY'), re.compile(r'GITHUB_TOKEN')]
        for p in patterns:
            match = p.search(SYSTEM_PROMPT)
            assert not match, f"credential pattern matched in system prompt: {match.group() if match else 'unknown'}"


# ---------------------------------------------------------------------------
# Agent-core skills tests
# ---------------------------------------------------------------------------

class TestAgentCoreSkills:
    def test_agent_core_skills_defined(self):
        assert "core" in AGENT_CORE_SKILLS
        assert "testing" in AGENT_CORE_SKILLS
        assert "git" in AGENT_CORE_SKILLS
        assert "github" in AGENT_CORE_SKILLS
        assert "governance" in AGENT_CORE_SKILLS
        assert "python" not in AGENT_CORE_SKILLS
        assert "react" not in AGENT_CORE_SKILLS
        assert "api" not in AGENT_CORE_SKILLS
        assert "database" not in AGENT_CORE_SKILLS

    def test_available_agent_skills_lists_all(self):
        skills = available_agent_skills()
        for name in AGENT_CORE_SKILLS:
            assert name in skills, f"missing agent skill: {name}"

    def test_load_agent_skill_returns_content(self):
        content = load_agent_skill("core")
        assert content is not None
        assert len(content) > 50

    def test_load_agent_skill_missing_returns_none(self):
        assert load_agent_skill("nonexistent_skill_xyz") is None

    def test_load_agent_skills_all(self):
        result = load_agent_skills()
        for name in AGENT_CORE_SKILLS:
            assert name in result, f"missing: {name}"

    def test_all_agent_skills_load_without_warnings(self, caplog):
        caplog.set_level(logging.WARNING)
        load_agent_skills()
        warnings = [r for r in caplog.records if "not found" in r.message]
        assert not warnings, f"unexpected warnings: {[r.message for r in warnings]}"

    def test_agent_skills_dir_resolves_independent_of_cwd(self):
        assert AGENT_SKILLS_DIR.is_dir()
        for name in AGENT_CORE_SKILLS:
            assert (AGENT_SKILLS_DIR / f"{name}.md").is_file(), f"missing: {name}"


# ---------------------------------------------------------------------------
# Repository-local skill discovery tests
# ---------------------------------------------------------------------------

class TestRepositorySkills:
    def test_no_skills_in_empty_workspace(self, tmp_path):
        skills = discover_repository_skills(tmp_path)
        assert not skills

    def test_skills_in_skills_dir(self, tmp_path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "nextjs.md").write_text("# Next.js rules")
        skills = discover_repository_skills(tmp_path)
        assert "nextjs" in skills

    def test_skills_in_dot_agent_skills(self, tmp_path):
        skill_dir = tmp_path / ".agent" / "skills"
        skill_dir.mkdir(parents=True)
        (skill_dir / "testing.md").write_text("# Testing rules")
        skills = discover_repository_skills(tmp_path)
        assert "testing" in skills

    def test_skills_in_dot_agents_skills(self, tmp_path):
        skill_dir = tmp_path / ".agents" / "skills"
        skill_dir.mkdir(parents=True)
        (skill_dir / "deploy.md").write_text("# Deploy rules")
        skills = discover_repository_skills(tmp_path)
        assert "deploy" in skills

    def test_load_repository_skills_returns_content(self, tmp_path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "frontend.md").write_text("# Frontend conventions")
        result = load_repository_skills(tmp_path)
        assert "frontend" in result
        assert "Frontend" in result["frontend"]

    def test_load_repository_skills_empty(self, tmp_path):
        assert not load_repository_skills(tmp_path)


# ---------------------------------------------------------------------------
# Technology detection tests
# ---------------------------------------------------------------------------

class TestTechnologyDetection:
    def test_no_indicators(self, tmp_path):
        assert detect_technologies(tmp_path) == []

    def test_pyproject_toml_detects_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        techs = detect_technologies(tmp_path)
        assert "python" in techs

    def test_package_json_detects_js_and_node(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"test"}')
        techs = detect_technologies(tmp_path)
        assert "js" in techs
        assert "node" in techs

    def test_next_config_detects_nextjs(self, tmp_path):
        (tmp_path / "next.config.ts").write_text("export default {}")
        techs = detect_technologies(tmp_path)
        assert "nextjs" in techs

    def test_multiple_indicators(self, tmp_path):
        (tmp_path / "package.json").write_text('{}')
        (tmp_path / "tsconfig.json").write_text('{}')
        techs = detect_technologies(tmp_path)
        assert "js" in techs
        assert "node" in techs
        assert "typescript" in techs

    def test_dockerfile_detected(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM node")
        techs = detect_technologies(tmp_path)
        assert "docker" in techs


# ---------------------------------------------------------------------------
# AGENTS.md discovery tests
# ---------------------------------------------------------------------------

class TestAgentsMdDiscovery:
    def test_find_no_agents_md(self, tmp_path):
        assert find_agents_md(tmp_path) == []

    def test_find_agents_md_in_workspace(self, tmp_path):
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# Test")
        found = find_agents_md(tmp_path)
        assert len(found) == 1
        assert found[0] == agents

    def test_find_agents_md_in_repository_subdir(self, tmp_path):
        repo_dir = tmp_path / "repository"
        repo_dir.mkdir()
        agents = repo_dir / "AGENTS.md"
        agents.write_text("# Test")
        found = find_agents_md(tmp_path)
        assert len(found) == 1
        assert found[0] == agents

    def test_find_nested_agents_md(self, tmp_path):
        repo_dir = tmp_path / "repository"
        repo_dir.mkdir()
        nested = repo_dir / "src" / "AGENTS.md"
        nested.parent.mkdir()
        nested.write_text("# Nested")
        found = find_agents_md(tmp_path)
        assert len(found) == 1

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
# Context builder tests (repository-agnostic)
# ---------------------------------------------------------------------------

class TestContextBuilder:
    def test_context_includes_repository_section(self):
        from app.agent.runner import build_context
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            context = build_context("my task", "TEST-1", "https://github.com/org/repo.git", Path(d), "main", None)
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

    def test_context_includes_agent_skills(self):
        from app.agent.runner import build_context
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            context = build_context("my task", "TEST-3", "https://github.com/org/repo.git", Path(d), "main", None)
            assert "Agent Skills" in context
            assert "### From" not in context  # no AGENTS.md

    def test_context_includes_technologies_when_detected(self, tmp_path):
        from app.agent.runner import build_context
        (tmp_path / "package.json").write_text('{}')
        (tmp_path / "tsconfig.json").write_text('{}')
        context = build_context("my task", "TEST-4", "https://github.com/org/repo.git", tmp_path, "main", None)
        assert "Detected Technologies" in context
        assert "typescript" in context

    def test_non_python_repository_no_python_assumption(self, tmp_path):
        """A non-Python repo should NOT have Python in the context."""
        from app.agent.runner import build_context
        (tmp_path / "package.json").write_text('{"name":"test"}')
        (tmp_path / "next.config.ts").write_text("export default {}")
        context = build_context("implement a feature", "TEST-5", "https://github.com/org/frontend.git", tmp_path, "main", None)
        # "python" appears in the tmp_path (function name), so check for the technology label
        assert "## Detected Technologies" in context
        assert "python" not in context.split("## Detected Technologies")[1].split("##")[0].lower()
        assert "React" not in context  # only Python/react would appear if detection incorrectly loads those skills

    def test_repository_skills_in_context(self, tmp_path):
        from app.agent.runner import build_context
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "frontend.md").write_text("# Frontend conventions\nUse named exports.")
        context = build_context("my task", "TEST-6", "https://github.com/org/repo.git", tmp_path, "main", None)
        assert "Repository Skills" in context
        assert "Frontend conventions" in context
        assert "Use named exports" in context

    def test_context_does_not_mix_agent_and_repo_skills(self, tmp_path):
        from app.agent.runner import build_context
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "core.md").write_text("# Repo-specific core")
        context = build_context("task", "TEST-7", "https://github.com/org/repo.git", tmp_path, "main", None)
        # Both agent-core and repo skills should be present but in separate sections
        assert "Agent Skills" in context
        assert "Repository Skills" in context


# ---------------------------------------------------------------------------
# Non-Python repository integration test
# ---------------------------------------------------------------------------

class TestNonPythonRepository:
    def test_nextjs_repository_no_python_assumptions(self, tmp_path):
        """A Next.js repository must not get Python-specific context from the agent.

        Agent-core skills may mention Python in their generic form (e.g. testing.md
        says 'for Python projects: pytest...' as a general instruction). That is
        acceptable — the agent must not assume the *target* repository is Python.
        The check here is that the detected technologies section is correct.
        """
        from app.agent.runner import build_context
        (tmp_path / "package.json").write_text('{"name":"next-app"}')
        (tmp_path / "next.config.ts").write_text("export default {}")
        (tmp_path / "tsconfig.json").write_text('{}')
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# Next.js project\nUse npm for package management.")
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "frontend.md").write_text("# Frontend rules\nUse TypeScript.\nUse functional components.")
        context = build_context("add a new page", "TEST-NEXT", "https://github.com/org/next-app.git", tmp_path, "main", None)
        # Must contain repository-specific context
        assert "Next.js project" in context
        assert "frontend" in context.lower()
        assert "TypeScript" in context
        # Detected technologies section must NOT list python
        if "## Detected Technologies" in context:
            tech_section = context.split("## Detected Technologies")[1].split("##")[0].lower()
            assert "python" not in tech_section
        # The Repository Skills section must NOT mention Python
        if "## Repository Skills" in context:
            repo_skills_section = context.split("## Repository Skills")[1].split("##")[0].lower()
            assert "python" not in repo_skills_section


# ---------------------------------------------------------------------------
# Security: no secrets in instruction files
# ---------------------------------------------------------------------------

class TestInstructionSecrets:
    def test_no_secrets_in_agent_skill_files(self):
        import re
        patterns = [re.compile(r'sk-[A-Za-z0-9]{10,}'), re.compile(r'lin-api-[A-Za-z0-9]+'),
                     re.compile(r'ghp_[A-Za-z0-9]{10,}'), re.compile(r'gho_[A-Za-z0-9]{10,}'),
                     re.compile(r'NEURON_API_KEY'), re.compile(r'LINEAR_API_KEY'), re.compile(r'GITHUB_TOKEN')]
        for f in AGENT_SKILLS_DIR.glob("*.md"):
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