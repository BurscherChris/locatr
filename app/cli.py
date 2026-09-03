import argparse
import asyncio
import json
import sys
import pytest
from app.agent.runner import AgentRunner
from app.config import get_settings

def main() -> None:
    parser = argparse.ArgumentParser(prog="agent")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--repo", required=True); run.add_argument("--issue", required=True); run.add_argument("--task", required=True); run.add_argument("--base-branch", default="main")
    commands.add_parser("health"); commands.add_parser("test")
    args = parser.parse_args()
    if args.command == "health": print(json.dumps({"status":"ok"})); return
    if args.command == "test": sys.exit(pytest.main(["tests"]))
    result = asyncio.run(AgentRunner(get_settings()).run(args.repo,args.issue,args.task,args.base_branch))
    print(json.dumps(result, indent=2))

if __name__ == "__main__": main()
