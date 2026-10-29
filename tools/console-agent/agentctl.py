#!/usr/bin/env python3
"""Host client for the n64to3ds console agent (tools/console-agent).

Examples:
  agentctl.py ping
  agentctl.py deploy out/HM64-A-async.cia --launch
  agentctl.py wait --timeout 900 && agentctl.py get /3ds/DaedalusX64/daedalus.log A.log

The console address comes from --host or $N64TO3DS_AGENT (host or host:port).
Only the standard library is used; nothing is contacted except the agent.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

DEFAULT_HOST = "192.168.1.24"
DEFAULT_PORT = 5001
CHUNK = 64 * 1024


class AgentError(Exception):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class Agent:
    def __init__(self, host: str, port: int, timeout: float = 15.0) -> None:
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise AgentError(
                f"Cannot connect to the agent at {host}:{port}: {exc}",
                hint="Start 'n64to3ds agent' on the console and check that Wi-Fi is on.",
            ) from exc
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.reader = self.sock.makefile("rb")

    def close(self) -> None:
        self.reader.close()
        self.sock.close()

    def __enter__(self) -> "Agent":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _line(self) -> str:
        raw = self.reader.readline()
        if not raw:
            raise AgentError("The agent closed the connection.")
        return raw.decode("ascii", "replace").rstrip("\r\n")

    def command(self, line: str, timeout: float | None = 15.0) -> str:
        self.sock.settimeout(timeout)
        self.sock.sendall(line.encode("ascii") + b"\n")
        return self._ok(self._line())

    @staticmethod
    def _ok(reply: str) -> str:
        if reply == "OK":
            return ""
        if reply.startswith("OK "):
            return reply[3:]
        raise AgentError(f"Agent error: {reply[4:] if reply.startswith('ERR ') else reply}")

    def _listing(self, line: str) -> list[str]:
        self.command(line)
        rows: list[str] = []
        while (row := self._line()) != ".":
            rows.append(row)
        return rows

    def ping(self) -> str:
        return self.command("PING")

    def ls(self, path: str) -> list[str]:
        return self._listing(f"LS {path}")

    def titles(self) -> list[str]:
        return self._listing("TITLES")

    def put(self, local: Path, remote: str) -> None:
        size = local.stat().st_size
        self.sock.settimeout(120)
        self.sock.sendall(f"PUT {remote} {size}\n".encode("ascii"))
        start, sent = time.monotonic(), 0
        with local.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                self.sock.sendall(chunk)
                sent += len(chunk)
                _progress("put", sent, size, start)
        print(file=sys.stderr)
        self._ok(self._line())

    def get(self, remote: str, local: Path) -> int:
        size = int(self.command(f"GET {remote}", timeout=60))
        tmp = local.with_name(local.name + ".part")
        received = 0
        with tmp.open("wb") as handle:
            while received < size:
                chunk = self.reader.read(min(CHUNK, size - received))
                if not chunk:
                    raise AgentError(f"Transfer of {remote} stopped after {received} of {size} bytes.")
                handle.write(chunk)
                received += len(chunk)
        tmp.replace(local)
        return size


def _progress(label: str, done: int, total: int, start: float) -> None:
    rate = done / max(time.monotonic() - start, 1e-6) / 1024
    print(f"\r{label} {done // 1024}/{total // 1024} KiB ({rate:.0f} KiB/s)", end="", file=sys.stderr)


def _address(args: argparse.Namespace) -> tuple[str, int]:
    value = args.host or os.environ.get("N64TO3DS_AGENT") or DEFAULT_HOST
    host, _, port = value.partition(":")
    return host, int(port) if port else args.port


def _connect(args: argparse.Namespace) -> Agent:
    return Agent(*_address(args))


def cmd_deploy(args: argparse.Namespace) -> None:
    local = Path(args.cia)
    if not local.is_file():
        raise AgentError(f"{local} does not exist.")
    remote = f"/cias/{local.name}"
    with _connect(args) as agent:
        agent.put(local, remote)
        print(f"uploaded {remote}")
        title = agent.command(f"INSTALL {remote}", timeout=900)
        print(f"installed {title}")
        if args.launch:
            agent.command(f"LAUNCH {title}")
            print(f"launched {title}")


def cmd_wait(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout
    host, port = _address(args)
    while True:
        try:
            with Agent(host, port, timeout=3) as agent:
                print(agent.ping())
                return
        except (AgentError, OSError):
            if time.monotonic() > deadline:
                raise AgentError(
                    f"The agent did not answer within {args.timeout} s.",
                    hint="The game may still be running, or the console went to sleep.",
                )
            time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help=f"console address (default $N64TO3DS_AGENT or {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    sub.add_parser("titles", help="list installed n64to3ds titles")
    p = sub.add_parser("ls"); p.add_argument("path")
    p = sub.add_parser("get"); p.add_argument("remote"); p.add_argument("local")
    p = sub.add_parser("put"); p.add_argument("local"); p.add_argument("remote")
    p = sub.add_parser("rm"); p.add_argument("remote")
    p = sub.add_parser("install"); p.add_argument("remote")
    p = sub.add_parser("launch"); p.add_argument("title_id")
    p = sub.add_parser("deploy", help="upload a CIA to /cias, install it, optionally launch it")
    p.add_argument("cia"); p.add_argument("--launch", action="store_true")
    p = sub.add_parser("wait", help="wait until the agent answers again")
    p.add_argument("--timeout", type=float, default=600)
    sub.add_parser("exit", help="quit the agent to the HOME Menu")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "deploy":
            cmd_deploy(args)
        elif args.cmd == "wait":
            cmd_wait(args)
        else:
            with _connect(args) as agent:
                if args.cmd == "ping":
                    print(agent.ping())
                elif args.cmd == "titles":
                    print("\n".join(agent.titles()))
                elif args.cmd == "ls":
                    print("\n".join(agent.ls(args.path)))
                elif args.cmd == "get":
                    print(f"{agent.get(args.remote, Path(args.local))} bytes -> {args.local}")
                elif args.cmd == "put":
                    agent.put(Path(args.local), args.remote)
                elif args.cmd == "rm":
                    agent.command(f"DEL {args.remote}")
                elif args.cmd == "install":
                    print(agent.command(f"INSTALL {args.remote}", timeout=900))
                elif args.cmd == "launch":
                    agent.command(f"LAUNCH {args.title_id}")
                elif args.cmd == "exit":
                    agent.command("EXIT")
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
