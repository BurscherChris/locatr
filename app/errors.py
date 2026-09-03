class AgentError(Exception):
    """Base exception for expected agent failures."""


class ConfigurationError(AgentError): pass
class AuthenticationError(AgentError): pass
class NeuronError(AgentError): pass
class ToolExecutionError(AgentError): pass
class GitError(ToolExecutionError): pass
class GitHubError(AgentError): pass
class LinearError(AgentError): pass
class WorkspaceError(AgentError): pass
class WebhookValidationError(AgentError): pass
class AgentIterationLimitError(AgentError): pass
