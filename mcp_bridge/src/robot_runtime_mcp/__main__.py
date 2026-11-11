import argparse
import asyncio

from robot_runtime_mcp.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Expose an existing robot runtime through MCP stdio")
    parser.add_argument("--socket", required=True, help="Existing RuntimeServer Unix socket")
    parser.add_argument("--allow-skill", action="append", default=None, help="Expose only this skill; repeat as needed")
    parser.add_argument("--deny-all-skills", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    allowed = (set() if options.deny_all_skills else
               set(options.allow_skill) if options.allow_skill is not None else None)
    asyncio.run(serve(options.socket, allowed_skills=allowed))


if __name__ == "__main__":
    main()
