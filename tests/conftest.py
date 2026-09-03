from pathlib import Path
import subprocess
import pytest
from app.config import Settings

@pytest.fixture
def settings(tmp_path):
    return Settings(workspace_root=str(tmp_path / "workspaces"), neuron_api_key="test", github_token="token", linear_api_key="linear", linear_webhook_secret="secret", command_timeout_seconds=5, github_repo="")

@pytest.fixture
def git_repository(tmp_path):
    repo = tmp_path / "source"; repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "message.txt").write_text("before\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True); subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo
