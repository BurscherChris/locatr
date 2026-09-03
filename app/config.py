from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000
    neuron_base_url: str = "https://neuron.noser.com/v1"
    neuron_api_key: str = ""
    neuron_model: str = "gpt-4.1"
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    linear_api_key: str = ""
    linear_webhook_secret: str = ""
    linear_api_url: str = "https://api.linear.app/graphql"
    agent_max_iterations: int = Field(default=50, ge=1, le=200)
    workspace_root: str = "/workspaces"
    command_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    http_timeout_seconds: int = Field(default=60, ge=1, le=600)
    agent_git_name: str = "Neuron Coding Agent"
    agent_git_email: str = "neuron-agent@localhost"
    command_allowlist: str = "pytest,npm,python,python3,git,node,uv,pip,poetry,make,ls,find,cat,sed,head,tail,rg"
    command_denylist: str = "curl,wget,ssh,scp,docker,mount,sudo,env,printenv"

    @property
    def allowed_commands(self) -> set[str]: return {x.strip() for x in self.command_allowlist.split(",") if x.strip()}
    @property
    def denied_commands(self) -> set[str]: return {x.strip() for x in self.command_denylist.split(",") if x.strip()}

    def readiness(self) -> dict[str, bool]:
        return {"neuron": bool(self.neuron_api_key), "github": bool(self.github_token), "linear": bool(self.linear_api_key)}


@lru_cache
def get_settings() -> Settings:
    return Settings()
