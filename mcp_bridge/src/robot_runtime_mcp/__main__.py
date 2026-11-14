import argparse
import asyncio
import os

from robot_runtime_mcp.server import serve, serve_http


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expose an existing robot runtime through MCP (stdio or streamable HTTP)")
    parser.add_argument("--socket", required=True, help="Existing RuntimeServer Unix socket")
    parser.add_argument("--allow-skill", action="append", default=None, help="Expose only this skill; repeat as needed")
    parser.add_argument("--deny-all-skills", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("stdio", "http"), default="stdio",
                        help="Client-facing transport: stdio (default) or streamable HTTP at /mcp")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (http mode)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port (http mode)")
    parser.add_argument("--token", default=None, help="Bearer token (http mode); prefer --token-env")
    parser.add_argument("--token-env", default=None,
                        help="Environment variable holding the bearer token (http mode)")
    options = parser.parse_args()
    allowed = (set() if options.deny_all_skills else
               set(options.allow_skill) if options.allow_skill is not None else None)
    if options.mode == "stdio":
        asyncio.run(serve(options.socket, allowed_skills=allowed))
        return
    token = options.token
    if options.token_env is not None:
        token = os.environ.get(options.token_env)
    if not token or not token.strip():
        parser.error("HTTP mode requires a nonempty bearer token via --token or --token-env")
    asyncio.run(serve_http(
        options.socket, allowed_skills=allowed,
        host=options.host, port=options.port, token=token))


if __name__ == "__main__":
    main()
