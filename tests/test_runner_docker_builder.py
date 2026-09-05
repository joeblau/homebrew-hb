"""Exercise builder provisioning with isolated fake CLIs; no Docker/VM/network use."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runner-docker-builder"
MOCK = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ["MOCK_ROOT"])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps([name] + args) + "\n")
state = json.loads((root / "state.json").read_text())
def save():
    (root / "state.json").write_text(json.dumps(state))
def inspect(builder):
    item = state.get("builders", {}).get(builder)
    if item is None:
        sys.exit(1)
    print("Name:          " + builder)
    print("Driver:        " + item["driver"])
    print("Nodes:\nName:          " + builder + "0")
    print("Endpoint:      " + item["endpoint"])
    print("Status:        running\nPlatforms:     linux/arm64")
if name == "uname":
    print({"-s": "Darwin", "-m": state.get("arch", "arm64"), "-r": "25.0.0"}[args[0]])
elif name == "brew":
    if args == ["--prefix", "docker-buildx"]:
        print(root / "brew-buildx")
    elif args[:1] != ["install"]:
        sys.exit(99)
    elif state.get("install_makes_plugin_available"):
        state["plugin_available"] = True
        save()
elif name == "colima":
    if args == ["status", "default"]:
        sys.exit(0 if state.get("colima_running", False) else 1)
    if args[:2] == ["start", "default"]:
        state["colima_running"] = True
        save()
    else:
        sys.exit(99)
elif name == "docker":
    if args == ["--version"]:
        print("Docker version test")
    elif args == ["buildx", "version"]:
        plugin = pathlib.Path(os.environ["DOCKER_CONFIG"]) / "cli-plugins/docker-buildx"
        sys.exit(0 if state.get("plugin_available", True) or plugin.is_file() else 1)
    elif args[:1] == ["--host"]:
        assert args[2:] == ["info", "--format", "{{.OSType}}"]
        if state.get("unreachable"):
            sys.exit(1)
        print(state.get("remote_os", "linux"))
    elif args[:1] == ["--context"]:
        assert args[2:] == ["info"]
    elif args[:2] == ["buildx", "inspect"]:
        rest = [a for a in args[2:] if a != "--bootstrap"]
        inspect(rest[0] if rest else state.get("selected", "runner-remote"))
    elif args[:2] == ["buildx", "create"]:
        builder = args[args.index("--name") + 1]
        assert builder not in state.get("builders", {}), "duplicate builder creation"
        state.setdefault("builders", {})[builder] = {
            "driver": args[args.index("--driver") + 1], "endpoint": args[-1]
        }
        save()
    elif args[:2] == ["buildx", "use"]:
        state["selected"] = args[-1]
        save()
    elif args == ["buildx", "ls"]:
        print("NAME/NODE DRIVER/ENDPOINT STATUS")
        print(state.get("selected", "runner-remote") + "* docker-container")
    elif args == ["context", "show"]:
        print(state.get("context", "desktop-linux"))
    elif args == ["context", "inspect", "desktop-linux"]:
        sys.exit(0 if state.get("desktop_available", True) else 1)
    elif args == ["context", "use", "desktop-linux"]:
        state["context"] = "desktop-linux"
        save()
    else:
        sys.exit(99)
else:
    sys.exit(99)
'''


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("docker", "brew", "colima", "uname"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + MOCK)
            path.chmod(0o755)
        plugin = self.root / "brew-buildx/bin/docker-buildx"
        plugin.parent.mkdir(parents=True)
        plugin.write_text("#!/bin/sh\nexit 0\n")
        plugin.chmod(0o755)
        self.config = self.root / "custom docker config"
        self.config.mkdir()
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "DOCKER_CONFIG": str(self.config),
            "MOCK_ROOT": str(self.root),
        }
        self.write_state({})

    def write_state(self, state):
        (self.root / "state.json").write_text(json.dumps(state))

    def calls(self):
        log = self.root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def run_helper(self, *args, success=True):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), *args], env=self.env,
            text=True, capture_output=True, timeout=10,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stderr)
        return result

    def remote(self, **kwargs):
        return self.run_helper("setup-remote", "--host", "ssh://ci@builder:2222", **kwargs)

    def test_remote_uses_docker_container_and_preserves_ssh_port(self):
        self.remote()
        self.assertIn(["docker", "--host", "ssh://ci@builder:2222", "info", "--format", "{{.OSType}}"], self.calls())
        self.assertIn(["docker", "buildx", "create", "--name", "runner-remote", "--driver", "docker-container", "ssh://ci@builder:2222"], self.calls())

    def test_repeat_setup_reuses_warm_builder(self):
        self.remote()
        self.remote()
        creates = [c for c in self.calls() if c[:3] == ["docker", "buildx", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertFalse(any(c[:3] == ["docker", "buildx", "rm"] for c in self.calls()))

    def test_incompatible_builder_is_never_removed(self):
        for driver, endpoint in [("remote", "ssh://ci@builder:2222"), ("docker-container", "ssh://other")]:
            with self.subTest(driver=driver, endpoint=endpoint):
                self.write_state({"builders": {"runner-remote": {"driver": driver, "endpoint": endpoint}}})
                result = self.remote(success=False)
                self.assertIn("Existing builder and cache are unchanged", result.stderr)
                self.assertFalse(any(c[:3] in [["docker", "buildx", "rm"], ["docker", "buildx", "create"]] for c in self.calls()))

    def test_unreachable_or_non_linux_daemon_cannot_create_builder(self):
        for state in [{"unreachable": True}, {"remote_os": "windows"}]:
            with self.subTest(state=state):
                self.write_state(state)
                self.remote(success=False)
                self.assertFalse(any(c[:3] == ["docker", "buildx", "create"] for c in self.calls()))

    def test_plugin_is_linked_in_custom_config_without_overwriting_auth(self):
        self.write_state({"plugin_available": False})
        auth = self.config / "config.json"
        auth.write_text('{"auths":{"example.test":{}}}')
        self.remote()
        self.assertTrue((self.config / "cli-plugins/docker-buildx").is_symlink())
        self.assertEqual(auth.read_text(), '{"auths":{"example.test":{}}}')

    def test_existing_plugin_search_path_is_used_after_install(self):
        self.write_state({"plugin_available": False, "install_makes_plugin_available": True})
        self.remote()
        self.assertFalse((self.config / "cli-plugins").exists())

    def test_broken_existing_plugin_is_preserved(self):
        self.write_state({"plugin_available": False})
        plugin = self.config / "cli-plugins/docker-buildx"
        plugin.parent.mkdir()
        plugin.symlink_to("missing-existing-target")
        self.remote(success=False)
        self.assertEqual(os.readlink(plugin), "missing-existing-target")

    def test_colima_uses_explicit_context_and_vz_for_new_arm_vm(self):
        self.env["DOCKER_CONTEXT"] = "desktop-linux"
        self.run_helper("setup-colima")
        create = next(c for c in self.calls() if c[:3] == ["docker", "buildx", "create"])
        self.assertEqual(create[-1], "colima")
        start = next(c for c in self.calls() if c[:2] == ["colima", "start"])
        self.assertEqual(start[2], "default")
        self.assertIn("--cpus", start)
        self.assertIn("virtiofs", start)
        self.assertIn("vz", start)

    def test_existing_stopped_colima_keeps_vm_engine(self):
        profile = self.root / ".colima/default/colima.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text("vmType: qemu\n")
        self.run_helper("setup-colima")
        start = next(c for c in self.calls() if c[:2] == ["colima", "start"])
        self.assertNotIn("--vm-type", start)
        self.assertEqual(profile.read_text(), "vmType: qemu\n")

    def test_running_colima_is_not_restarted(self):
        self.write_state({"colima_running": True})
        result = self.run_helper("setup-colima", "--cpu", "12")
        self.assertIn("keeping its current resource allocation", result.stderr)
        self.assertFalse(any(c[:2] == ["colima", "start"] for c in self.calls()))

    def test_colima_rejects_builder_pointing_at_desktop(self):
        self.write_state({"colima_running": True, "builders": {"runner-colima": {"driver": "docker-container", "endpoint": "desktop-linux"}}})
        self.run_helper("setup-colima", success=False)
        self.assertFalse(any(c[:3] == ["docker", "buildx", "rm"] for c in self.calls()))

    def test_status_uses_current_builder_inspection(self):
        self.remote()
        result = self.run_helper("status")
        self.assertIn("selected builder: runner-remote", result.stderr)

    def test_missing_desktop_does_not_select_unrelated_default(self):
        self.write_state({"desktop_available": False})
        self.run_helper("use-desktop", success=False)
        self.assertFalse(any(c[:3] == ["docker", "buildx", "use"] for c in self.calls()))

    def test_invalid_size_fails_before_install_or_provisioning(self):
        self.run_helper("setup-colima", "--cpu", "0", success=False)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
