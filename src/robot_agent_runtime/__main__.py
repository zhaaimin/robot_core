"""Command-line entry points for the robot runtime UDS daemon.

Two backends share one transport:

- ``serve``    simulator-backed daemon (no hardware; tests and demos)
- ``serve-cc`` CC-platform daemon (real UBT robot; ``audio.speak``)

Both expose the same ``RuntimeServer`` over a Unix socket, so an MCP bridge
(``robot_runtime_mcp``) can sit in front of either without knowing which
backend it talks to.
"""

import argparse
import asyncio
import os
import signal
from collections.abc import Sequence

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.transport.server import RuntimeServer
from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository


def _build_serve_parser(subparsers) -> None:
    serve = subparsers.add_parser("serve", help="run the simulator-backed UDS daemon")
    serve.add_argument("--socket", required=True, help="path for the owned Unix socket")
    serve.add_argument("--database", required=True, help="SQLite task database path")
    serve.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="simulated delay in seconds for each skill (default: 0)",
    )


def _build_serve_cc_parser(subparsers) -> None:
    serve_cc = subparsers.add_parser(
        "serve-cc", help="run the CC-platform UDS daemon (real UBT robot)",
    )
    serve_cc.add_argument("--socket", required=True, help="path for the owned Unix socket")
    serve_cc.add_argument("--database", required=True, help="SQLite task database path")
    serve_cc.add_argument("--api-id", required=True, help="registered robot API client ID")
    serve_cc.add_argument("--address", help="robot API host:port; omit when running on the robot")
    serve_cc.add_argument(
        "--token-env", default="CC_API_TOKEN",
        help="environment variable holding the CC API token (default: CC_API_TOKEN)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robot-agent-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _build_serve_parser(subparsers)
    _build_serve_cc_parser(subparsers)
    return parser


async def serve(socket_path: str, database_path: str, delay_s: float) -> None:
    if delay_s < 0:
        raise ValueError("--delay must be non-negative")
    simulator = Simulator(delay_s=delay_s)
    server = RuntimeServer(SQLiteTaskRepository(database_path), simulator.skills, socket_path)
    await _run_server(server, socket_path)


async def serve_cc(
    socket_path: str, database_path: str, api_id: str,
    address: str | None, token: str,
) -> None:
    """Serve the CC platform over UDS; the caller keeps the native connection.

    The platform is started before the server and closed after it, mirroring
    ``RobotRuntime.from_platform``'s lifecycle ordering. The native
    ``ubt_robot`` API is connected synchronously (its ``start`` and
    ``wait_until_connected`` are blocking) before the asyncio server runs.
    """
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
    from robot_agent_runtime.errors import RobotConnectionError

    try:
        import ubt_robot
    except ImportError as error:
        raise RobotConnectionError(
            "Install the supplied ubt_robot Linux ARM64 wheel in the deployment environment"
        ) from error

    config = ubt_robot.Config()
    config.api_id = api_id
    config.is_reconnect = False
    if address:
        config.address = address
        config.token = token
    api = ubt_robot.Api()
    api.initialize(config)
    try:
        api.start()
        if not api.wait_until_connected(5000):
            raise RobotConnectionError("CC API did not connect within 5 seconds")
        platform = create_cc_api_platform(api, skill_thread_group="audio")
        server = RuntimeServer(
            SQLiteTaskRepository(database_path), platform.skills, socket_path,
        )
        try:
            await platform.start()
            await _run_server(server, socket_path)
        finally:
            await platform.close()
    finally:
        api.stop()


async def _run_server(server: RuntimeServer, socket_path: str) -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    await server.start()
    try:
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_name, stopped.set)
                registered_signals.append(signal_name)
            except (NotImplementedError, RuntimeError):
                pass
        print(f"Robot Agent Runtime listening on {socket_path}", flush=True)
        await stopped.wait()
    finally:
        for signal_name in registered_signals:
            loop.remove_signal_handler(signal_name)
        await server.close()


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            asyncio.run(serve(args.socket, args.database, args.delay))
        elif args.command == "serve-cc":
            token = os.environ.get(args.token_env, "")
            asyncio.run(serve_cc(args.socket, args.database, args.api_id, args.address, token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
