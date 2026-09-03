import pytest
from app.tools.shell import ShellTools
from app.errors import ToolExecutionError

@pytest.mark.asyncio
async def test_shell_policy_and_timeout(tmp_path):
    tools = ShellTools(tmp_path, {"python3"}, {"curl"}, 1)
    assert (await tools.run_command("python3 -c 'print(1)'"))["exit_code"] == 0
    with pytest.raises(ToolExecutionError): await tools.run_command("curl example.com")
    with pytest.raises(ToolExecutionError): await tools.run_command("python3 -c 'import time; time.sleep(2)'")
