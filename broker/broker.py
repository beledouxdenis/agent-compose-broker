#!/usr/bin/env python3
import argparse
import logging
import os
import pty
import select
import signal
import socketserver
import subprocess
import uuid
from pathlib import Path

import yaml

SOCKET = Path(os.environ.get("COMPOSE_BROKER_SOCKET", "/broker/compose.sock"))
COMPOSE_FILE = Path(os.environ.get("COMPOSE_BROKER_FILE", "/services/compose.yml"))

ENGINE_COMMANDS = {
    "docker": (["docker", "compose"], "DOCKER_HOST", "unix:///run/engines/docker.sock"),
    "podman": (["podman-compose"], "CONTAINER_HOST", "unix:///run/engines/podman.sock"),
}


class DeniedCommand(Exception):
    pass


class StrictArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise DeniedCommand(message)


RUN_PARSER = StrictArgumentParser(prog="compose run", add_help=False)
RUN_PARSER.add_argument("--rm", action="store_true")
RUN_PARSER.add_argument("--no-TTY", "-T", action="store_true")
RUN_PARSER.add_argument("--env", "-e", action="append", default=[])
RUN_PARSER.add_argument("service")
RUN_PARSER.add_argument("command", nargs=argparse.REMAINDER)

ACTIVE_CONTAINERS = {}
LOGGER = logging.getLogger(__name__)


def load_services(compose_file=COMPOSE_FILE):
    with compose_file.open(encoding="utf-8") as stream:
        compose = yaml.safe_load(stream) or {}
    services = compose.get("services")
    if not isinstance(services, dict) or not services:
        raise RuntimeError(f"no services declared in {compose_file}")
    return frozenset(services)


def engine_environment(engine):
    _command, variable, default = ENGINE_COMMANDS[engine]
    configured = os.environ.get(f"COMPOSE_BROKER_{engine.upper()}_HOST", default)
    return {variable: configured}


def prepare_command(engine, argv, allowed_services):
    if engine not in ENGINE_COMMANDS or not argv:
        raise DeniedCommand

    compose_command, _variable, _default = ENGINE_COMMANDS[engine]
    environment = engine_environment(engine)

    if argv[0] == "exec":
        if len(argv) < 3 or ACTIVE_CONTAINERS.get(argv[1], (None, None))[0] != engine or argv[2].startswith("-"):
            raise DeniedCommand
        return [engine, *argv], None, None, False, environment

    if argv[0] == "inspect":
        if len(argv) != 2 or ACTIVE_CONTAINERS.get(argv[1], (None, None))[0] != engine:
            raise DeniedCommand
        return [engine, *argv], None, None, False, environment

    if argv[0] != "run":
        raise DeniedCommand

    args = RUN_PARSER.parse_args(argv[1:])
    if args.service not in allowed_services:
        raise DeniedCommand

    normalized = list(argv)
    if not args.rm:
        normalized.insert(1, "--rm")
    container_name = f"broker-{engine}-{args.service}-{uuid.uuid4().hex}"
    normalized[1:1] = ["--name", container_name]
    command = [*compose_command, "-f", str(COMPOSE_FILE), *normalized]
    return command, args.service, container_name, True, environment


class Handler(socketserver.BaseRequestHandler):
    def _read_line(self):
        data = bytearray()
        while True:
            chunk = self.request.recv(1)
            if not chunk or chunk == b"\n":
                break
            data.extend(chunk)
        return data.decode()

    def handle(self):
        engine = self._read_line()
        stdin_is_tty = self._read_line() == "1"
        argv = self._read_line().split("\0")
        try:
            command, service, container_name, stream, environment = prepare_command(
                engine, argv, self.server.allowed_services
            )
        except DeniedCommand:
            self.request.sendall(f"denied {engine} compose {' '.join(argv)}\n".encode())
            return
        self.run(
            engine,
            command,
            service=service,
            container_name=container_name,
            stdin_is_tty=stdin_is_tty,
            stream=stream,
            environment=environment,
        )

    def run(self, engine, command, service, container_name, stdin_is_tty, stream, environment):
        process_environment = dict(os.environ, **environment)
        if not stream:
            process = subprocess.run(
                command,
                cwd=COMPOSE_FILE.parent,
                env=process_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.request.sendall(process.stdout)
            return

        master, slave = pty.openpty() if stdin_is_tty else (None, None)
        process = subprocess.Popen(
            command,
            cwd=COMPOSE_FILE.parent,
            env=process_environment,
            stdin=slave if stdin_is_tty else subprocess.PIPE,
            stdout=slave if stdin_is_tty else subprocess.PIPE,
            stderr=slave if stdin_is_tty else subprocess.STDOUT,
            start_new_session=True,
        )
        if stdin_is_tty:
            os.close(slave)
        try:
            if container_name:
                ACTIVE_CONTAINERS[container_name] = (engine, service)
                self.request.sendall(f"broker container name: {container_name}\n".encode())
            output = master if stdin_is_tty else process.stdout.fileno()
            stdin_open = True
            readers = [self.request, output]
            while output in readers:
                for ready in select.select(readers, [], [])[0]:
                    if ready is self.request:
                        data = self.request.recv(65536)
                        if not data:
                            if stdin_is_tty:
                                process.terminate()
                                return
                            if stdin_open:
                                process.stdin.close()
                                stdin_open = False
                            readers.remove(self.request)
                            continue
                        if stdin_is_tty:
                            os.write(master, data)
                        elif stdin_open:
                            process.stdin.write(data)
                            process.stdin.flush()
                    else:
                        try:
                            data = os.read(output, 65536)
                        except OSError:
                            return
                        if not data:
                            readers.remove(output)
                            continue
                        self.request.sendall(data)
        finally:
            if container_name:
                ACTIVE_CONTAINERS.pop(container_name, None)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=15)
            if stdin_is_tty:
                os.close(master)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    with socketserver.ThreadingUnixStreamServer(str(SOCKET), Handler) as server:
        server.allowed_services = load_services()
        SOCKET.chmod(0o666)
        LOGGER.info("compose broker listening on %s; services: %s", SOCKET, ", ".join(sorted(server.allowed_services)))
        server.serve_forever()
