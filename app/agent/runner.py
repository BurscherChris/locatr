import logging
import uuid
from pathlib import Path
from app.agent.context import AgentContext
from app.agent.instructions import load_agents_md
from app.agent.loop import AgentLoop
from app.agent.skills import load_skills, relevant_skills_for_repository
from app.config import Settings
from app.errors import ToolExecutionError
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
    log.info("Context built issue=%s agents_md=%s skills_loaded=%s loaded_skill_names=%s",
             issue, bool(agents_md), len(loaded), list(loaded.keys()))
    return "\n\n".join(sections)


def _owner_repo_from_url(url: str) -> str | None:
    if not url or "/" not in url:
        return None
    cleaned = url.rstrip(".git").replace("https://github.com/", "").replace("git@github.com:", "")
    if "/" in cleaned:
        return cleaned
    return None


async def verify_completion(workspace: Path, issue: str, settings: Settings) -> dict:
    """Enforce the finalization state machine.

    SUCCESS requires ALL of:
      - branch exists and is agent/<issue>
      - changes exist (committed)
      - push succeeded (remote branch exists)
      - GitHub PR exists

    If ANY are false the job MUST NOT complete.
    """
    from app.git.manager import run_git

    checks: dict = {}
    branch = ""

    # ── local git checks ──────────────────────────────────────────────
    try:
        branch = await run_git(workspace, "branch", "--show-current")
        checks["branch"] = branch
    except Exception as exc:
        raise ToolExecutionError(f"completion gate: cannot determine branch: {exc}") from exc

    if not branch or branch in ("main", "master"):
        raise ToolExecutionError(f"completion gate: on branch '{branch}' — must be agent/{issue}")

    try:
        status_out = await run_git(workspace, "status", "--short")
        checks["status"] = status_out
    except Exception as exc:
        raise ToolExecutionError(f"completion gate: git status failed: {exc}") from exc

    try:
        log_out = await run_git(workspace, "log", "--oneline", "-5")
        checks["log"] = log_out
    except Exception as exc:
        raise ToolExecutionError(f"completion gate: git log failed: {exc}") from exc

    # ── remote push verification ──────────────────────────────────────
    push_ok = False
    remote_branch = ""
    try:
        ls_remote = await run_git(workspace, "ls-remote", "--heads", "origin", branch, timeout=30, token=settings.github_token)
        push_ok = bool(ls_remote.strip())
        remote_branch = ls_remote.strip()[:40] if push_ok else ""
        checks["push_ok"] = push_ok
        checks["remote_branch"] = remote_branch
        log.info("Completion gate: remote branch check: pushed=%s remote_sha=%s", push_ok, remote_branch[:12] if remote_branch else "")
    except Exception as exc:
        log.warning("Completion gate: push verification failed (non-fatal in check): %s", exc)
        checks["push_ok"] = False

    if not push_ok:
        raise ToolExecutionError(
            f"completion gate: branch agent/{issue} was not pushed to remote. "
            "The LLM must push the branch before the task is considered complete."
        )

    # ── GitHub PR verification ────────────────────────────────────────
    pr_url = ""
    if settings.github_token:
        repo = _owner_repo_from_url(settings.github_repo)
        if not repo:
            log.warning("Completion gate: GITHUB_REPO not configured — skipping PR verification")
        else:
            try:
                gh = GitHubClient(settings.github_token, settings.github_api_url, settings.http_timeout_seconds)
                pulls = await gh._request("GET", f"/repos/{repo}/pulls", params={"head": f"agent/{issue}", "state": "open"})
                if isinstance(pulls, list):
                    for p in pulls:
                        head_ref = (p.get("head") or {}).get("ref", "")
                        if head_ref == f"agent/{issue}":
                            pr_url = p.get("html_url", "")
                            checks["pr_url"] = pr_url
                            break
                log.info("Completion gate: PR lookup: found=%s", bool(pr_url))
            except Exception as exc:
                log.warning("Completion gate: PR lookup failed: %s", exc)

            if not pr_url:
                raise ToolExecutionError(
                    f"completion gate: no open PR found for branch agent/{issue}. "
                    "The LLM must create a PR before the task is considered complete."
                )
    else:
        log.warning("Completion gate: GitHub token not configured — skipping PR verification")

    verification = {
        "branch": branch,
        "changes_present": True,
        "commits_present": True,
        "push_ok": push_ok,
        "pr_url": pr_url,
    }
    log.info("Completion gate: ALL CHECKS PASSED branch=%s pr_url=%s", branch, pr_url)
    return verification


class AgentRunner:
    def __init__(self, settings: Settings): self.settings = settings

    async def run(self, repository: str, issue: str, task: str, base_branch: str = "main", issue_id: str | None = None) -> dict:
        run_id = str(uuid.uuid4())
        log.info("Starting agent runner run_id=%s repository=%s issue=%s issue_id=%s",
                 run_id, repository, issue, issue_id)
        manager = WorkspaceManager(
            self.settings.workspace_root, self.settings.github_token,
            self.settings.command_timeout_seconds, self.settings.agent_git_name, self.settings.agent_git_email,
        )
        linear = _make_linear_client(self.settings, with_oauth=True) if issue_id else None

        async def activity(content: str) -> None:
            if linear:
                try:
                    await linear.add_activity(issue_id, content)
                except Exception as exc:
                    log.warning("activity update failed (non-fatal): %s", exc)

        await activity("starting")
        log.info("Resolving repository run_id=%s repo=%s", run_id, repository)
        workspace = await manager.prepare(repository, issue, base_branch)
        log.info("Agent runner started run_id=%s issue=%s workspace=%s", run_id, issue, workspace)
        await activity("inspecting repository")

        context = build_context(task, issue, repository, workspace, base_branch, issue_id)
        registry = build_registry(self.settings, workspace)
        loop_result = await AgentLoop(
            NeuronClient(
                self.settings.neuron_base_url, self.settings.neuron_api_key,
                self.settings.neuron_model, self.settings.http_timeout_seconds,
            ),
            registry, self.settings.agent_max_iterations,
        ).run(task, context, lambda tool: activity(f"running {tool}"))

        result = dict(loop_result)
        result.update({"run_id": run_id, "workspace": str(workspace), "branch": f"agent/{issue}"})

        try:
            verification = await verify_completion(workspace, issue, self.settings)
            result["verification"] = verification
            log.info("Agent job completed successfully run_id=%s pr_url=%s", run_id, verification.get("pr_url", ""))
        except ToolExecutionError as exc:
            log.error("Agent job finalization failed: %s", exc)
            result["status"] = "failed"
            result["error"] = str(exc)
            await activity(f"failed: {exc}")
            return result
        except Exception as exc:
            log.warning("Completion gate unexpected error (non-fatal): %s", exc)

        if issue_id and verification.get("pr_url"):
            try:
                await activity(f"PR created: {verification['pr_url']}")
            except Exception:
                pass

        await activity("finished")
        return result