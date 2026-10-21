import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("COMPOSE_BROKER_FILE", "/services/compose.yml")
spec = importlib.util.spec_from_file_location("broker", Path(__file__).with_name("broker.py"))
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class ComposeCommandTest(unittest.TestCase):
    def setUp(self):
        broker.ACTIVE_CONTAINERS.clear()
        self.services = {"app", "database"}

    def assert_allowed(self, engine, *argv):
        return broker.prepare_command(engine, list(argv), self.services)

    def assert_denied(self, engine, *argv):
        with self.assertRaises(broker.DeniedCommand):
            broker.prepare_command(engine, list(argv), self.services)

    def test_loads_services_from_compose_file(self):
        with tempfile.TemporaryDirectory() as directory:
            compose_file = Path(directory) / "compose.yml"
            compose_file.write_text("services:\n  app:\n    image: example\n  database: {}\n")
            self.assertEqual(broker.load_services(compose_file), {"app", "database"})

    def test_run_uses_requested_engine_and_protected_file(self):
        podman = self.assert_allowed("podman", "run", "app", "test")
        docker = self.assert_allowed("docker", "run", "app", "test")
        self.assertEqual(podman[0][:3], ["podman-compose", "-f", str(broker.COMPOSE_FILE)])
        self.assertEqual(docker[0][:4], ["docker", "compose", "-f", str(broker.COMPOSE_FILE)])

    def test_run_forces_rm_and_generated_name(self):
        command, service, name, stream, _environment = self.assert_allowed("podman", "run", "app", "test")
        self.assertIn("--rm", command)
        self.assertEqual(command[command.index("--name") + 1], name)
        self.assertTrue(name.startswith("broker-podman-app-"))
        self.assertEqual(service, "app")
        self.assertTrue(stream)

    def test_declared_services_and_environment_are_allowed(self):
        self.assert_allowed("docker", "run", "database")
        self.assert_allowed("docker", "run", "-e", "VALUE=custom", "app", "./build")

    def test_unknown_service_engine_operation_and_options_are_denied(self):
        self.assert_denied("containerd", "run", "app")
        self.assert_denied("podman", "run", "unknown")
        self.assert_denied("podman", "build", "app")
        self.assert_denied("docker", "run", "--volume", "/:/host", "app")
        self.assert_denied("docker", "run", "--publish", "8080:80", "app")
        self.assert_denied("docker", "run", "--privileged", "app")

    def test_exec_and_inspect_are_bound_to_engine_and_active_container(self):
        broker.ACTIVE_CONTAINERS["generated-name"] = ("podman", "app")
        self.assert_allowed("podman", "inspect", "generated-name")
        self.assert_allowed("podman", "exec", "generated-name", "id")
        self.assert_denied("docker", "inspect", "generated-name")
        self.assert_denied("docker", "exec", "generated-name", "id")
        self.assert_denied("podman", "exec", "other-name", "id")


if __name__ == "__main__":
    unittest.main()
