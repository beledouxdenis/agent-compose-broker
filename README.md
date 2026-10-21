# agent-compose-broker

`agent-compose-broker` lets an AI coding agent run services from a protected
Compose file without giving the agent direct access to the Docker or Podman
socket.

The agent chooses the engine by using the familiar command:

```sh
podman compose run example
docker compose run example
```

Both commands are small clients which forward the request to the broker. The
broker accepts `run` only for services declared in `services/compose.yml`. It
also accepts `inspect` and `exec` only for containers that it started and only
through the same engine.

## Layout

```text
codex/                 Codex image and restricted Docker/Podman clients
broker/                Broker server, client and tests
services/compose.yml   Protected list of services available to the agent
compose.yml            User-facing stack which starts Codex and the broker
```

The two Compose files are intentionally separate. Neither `compose.yml` nor
`services/compose.yml` is mounted into the Codex container. In a consuming
project, only the source directories that Codex is expected to edit should be
mounted there.

## Run

Set the socket for each engine that should be available. An unset engine uses a
placeholder file, allowing Codex and the broker to start when that engine is
not installed. A request for an unavailable engine will fail when it is made.

### Linux with Podman

Enable the rootless Podman API socket and pass it to the broker:

```sh
systemctl --user enable --now podman.socket
export PODMAN_SOCKET="$XDG_RUNTIME_DIR/podman/podman.sock"
podman-compose run --rm codex
```

### Linux with Docker

```sh
export DOCKER_SOCKET=/var/run/docker.sock
docker compose run --rm codex
```

### macOS with Podman Machine

Start the Podman virtual machine and obtain its default SSH connection:

```sh
podman machine start

uri="$(podman system connection list --format '{{if .Default}}{{.URI}}{{end}}')"
key="$(podman system connection list --format '{{if .Default}}{{.Identity}}{{end}}')"

printf 'PODMAN_BROKER_HOST=%s\n' "${uri/127.0.0.1/host.containers.internal}" >> .env
printf 'PODMAN_SSHKEY=%s\n' "$key" >> .env

podman-compose run --rm codex
```

The address substitution is necessary because `127.0.0.1` in the broker
container refers to the broker itself. `host.containers.internal` reaches the
macOS host where Podman Machine exposes its forwarded SSH port.

### macOS with Docker Desktop

Docker Desktop exposes its engine socket to containers at the conventional
path. Start Docker Desktop, then run:

```sh
export DOCKER_SOCKET=/var/run/docker.sock
docker compose run --rm codex
```

An engine whose socket or SSH connection is not configured is simply
unavailable to requests from Codex.

## Security model

The protected `services/compose.yml` is the allowlist. It controls images,
build contexts, mounts, networks, devices and privileges. The broker always
passes that file explicitly to Compose; callers cannot select another file,
image, mount, port or privileged option.

The protected file and every file it references must not be writable by the
agent. Environment interpolation in it is performed with the broker's trusted
environment, not environment variables supplied by the client.
