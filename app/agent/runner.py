import logging
import uuid
from pathlib import Path
from app.agent.context import AgentContext
from app.agent.instructions import load_agents_md
from app.agent.loop import AgentLoop
from app.agent.skills import load_skills, relevant_skills_for_repository
from app.config import Settings
from app.github.client import GitHubClient
from app.linear.client import LinearClient
from app.neuron.client import NeuronClient
from app.tools.base import Tool
from app.tools.filesystem import FilesystemTools
from app.tools.git import GitTools
from app.tools.github import GitHubTools
from app.tools.linear import LinearTools
from app.tools.registry import ToolRegistry
from app.tools.shell import ShellTools
from app.workspace.manager import WorkspaceManager


def _linear_token_manager(settings: Settings):
    if not settings.linear_client_id or not settings.linear_client_secret:
        return None
    from app.linear.oauth import LinearOAuthClient, LinearTokenFileStore, LinearTokenManager
    client = LinearOAuthClient(
        settings.linear_client_id, settings.linear_client_secret,
        settings.linear_oauth_redirect_uri, settings.http_timeout_seconds,
    )
    store = LinearTokenFileStore(settings.linear_token_store_path)
    return LinearTokenManager(client, store)


def _make_linear_client(settings: Settings, with_oauth: bool = False) -> LinearClient:
    token_mgr = _linear_token_manager(settings) if with_oauth else None
    return LinearClient(settings.linear_api_key, settings.linear_api_url, settings.http_timeout_seconds, token_mgr)


log = logging.getLogger(__name__)
P = lambda props, required=[]: {"type":"object","properties":props,"required":required,"additionalProperties":False}
S = lambda desc: {"type":"string","description":desc}


def build_registry(settings: Settings, workspace, include_remote: bool = True) -> ToolRegistry:
    registry, fs = ToolRegistry(), FilesystemTools(workspace)
    shell, git = ShellTools(workspace, settings.allowed_commands, settings.denied_commands, settings.command_timeout_seconds), GitTools(workspace, settings.github_token, settings.command_timeout_seconds)
    for name, desc, schema, func in [
        ("read_file","Read a workspace file",P({"path":S("relative path")},["path"]),fs.read_file), ("write_file","Write a workspace file",P({"path":S("relative path"),"content":S("complete content")},["path","content"]),fs.write_file), ("list_files","List workspace directory",P({"path":S("relative path")}),fs.list_files), ("search_code","Search text in code",P({"query":S("text"),"path":S("relative path")},["query"]),fs.search_code),
        ("run_command","Run an allowed development command",P({"command":S("command"),"timeout_seconds":{"type":"integer","minimum":1}},["command"]),shell.run_command), ("run_tests","Run tests",P({"command":S("test command")}),shell.run_tests),
        ("git_status","Show git status",P({}),git.git_status), ("git_diff","Show git diff",P({}),git.git_diff), ("git_log","Show commits",P({"limit":{"type":"integer","minimum":1,"maximum":50}}),git.git_log), ("git_create_branch","Create agent branch",P({"branch":S("branch")},["branch"]),git.git_create_branch), ("git_commit","Commit all changes",P({"message":S("commit message")},["message"]),git.git_commit), ("git_push","Push branch",P({"branch":S("branch")},["branch"]),git.git_push)]: registry.register(Tool(name,desc,schema,func))
    if include_remote:
        gh = GitHubTools(GitHubClient(settings.github_token, settings.github_api_url, settings.http_timeout_seconds))
        linear = LinearTools(_make_linear_client(settings, with_oauth=True))
        for name, desc, schema, func in [("create_pull_request","Create GitHub PR",P({"repository":S("owner/repo"),"title":S("title"),"head":S("branch"),"base":S("base branch"),"body":S("body")},["repository","title","head","base","body"]),gh.create_pull_request),("get_pull_request","Get GitHub PR",P({"repository":S("owner/repo"),"number":{"type":"integer"}},["repository","number"]),gh.get_pull_request),("update_linear_issue","Update Linear issue",P({"issue_id":S("id"),"state_id":S("state"),"description":S("description")},["issue_id"]),linear.update_linear_issue),("add_linear_comment","Comment on Linear issue",P({"issue_id":S("id"),"body":S("comment")},["issue_id","body"]),linear.add_linear_comment),("add_linear_activity","Post Linear activity",P({"issue_id":S("id"),"content":S("activity")},["issue_id","content"]),linear.add_linear_activity)]: registry.register(Tool(name,desc,schema,func))
    return registry


def build_context(task: str, issue: str, repository: str, workspace: Path, base_branch: str, issue_id: str | None) -> str:
    """Build the layered agent context.

    Sections are separated clearly:
      SYSTEM — agent identity and immutable rules
      REPOSITORY — workspace metadata
      REPOSITORY INSTRUCTIONS — AGENTS.md content if present
      SKILLS — relevant loaded skills
      TASK — the Linear issue task
      TOOLS — available tools (added by the loop at runtime)
      WORKFLOW — phase expectations
    """
    agents_md = load_agents_md(workspace, issue)

    skill_names = relevant_skills_for_repository(repository, task)
    loaded = load_skills(skill_names)
    skills_text = "\n\n".join(loaded.values())

    sections = []

    sections.append(f"## Repository\nRepository: {repository}\nIssue: {issue}\nBranch: agent/{issue}\nBase branch: {base_branch}\nWorkspace: {workspace}")

    if agents_md:
        sections.append(f"## Repository Instructions (AGENTS.md)\n{agents_md}")

    if skills_text:
        sections.append(f"## Skills\n{skills_text}")

    sections.append(f"## Task\n{task}")

    sections.append("## Workflow\nFollow the system prompt workflow. Do not skip validation. Inspect the diff before committing.")

    log.info("Context built issue=%s agents_md=%s skills=%s", issue, bool(agents_md), skill_names)

    return "\n\n".join(sections)


class AgentRunner:
    def __init__(self, settings: Settings): self.settings = settings
    async def run(self, repository: str, issue: str, task: str, base_branch: str = "main", issue_id: str | None = None) -> dict:
        run_id = str(uuid.uuid4())
        log.info("Starting agent runner run_id=%s repository=%s issue=%s issue_id=%s",
                 run_id, repository, issue, issue_id)
        manager = WorkspaceManager(self.settings.workspace_root, self.settings.github_token, self.settings.command_timeout_seconds, self.settings.agent_git_name, self.settings.agent_git_email)
        linear = _make_linear_client(self.settings, with_oauth=True) if issue_id else None
        async def activity(content: str) -> None:
            if linear: await linear.add_activity(issue_id, content)
        await activity("starting")
        log.info("Resolving repository run_id=%s repo=%s", run_id, repository)
        workspace = await manager.prepare(repository, issue, base_branch)
        log.info("Agent runner started run_id=%s issue=%s workspace=%s", run_id, issue, workspace)
        await activity("inspecting repository")
        context = build_context(task, issue, repository, workspace, base_branch, issue_id)
        registry = build_registry(self.settings, workspace)
        result = await AgentLoop(NeuronClient(self.settings.neuron_base_url,self.settings.neuron_api_key,self.settings.neuron_model,self.settings.http_timeout_seconds), registry, self.settings.agent_max_iterations).run(task, context, lambda tool: activity(f"running {tool}"))
        result.update({"run_id":run_id,"workspace":str(workspace),"branch":f"agent/{issue}"})
        await activity("finished")
        return result