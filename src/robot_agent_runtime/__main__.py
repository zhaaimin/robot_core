"""Command-line entry point for the simulator-backed UDS daemon."""

import argparse
import asyncio
import signal
from collections.abc import Sequence

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.server import RuntimeServer
from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robot-agent-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="run the simulator-backed UDS daemon")
    serve.add_argument("--socket", required=True, help="path for the owned Unix socket")
    serve.add_argument("--database", required=True, help="SQLite task database path")
    serve.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="simulated delay in seconds for each skill (default: 0)",
    )
    return parser


async def serve(socket_path: str, database_path: str, delay_s: float) -> None:
    if delay_s < 0:
        raise ValueError("--delay must be non-negative")
    simulator = Simulator(delay_s=delay_s)
    core = RuntimeCore(SQLiteTaskRepository(database_path), simulator.skills)
    server = RuntimeServer(core, socket_path)
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
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
