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
    elif args[:1] == ["ssh"]:
        (root / "vm_stdin.txt").write_text(sys.stdin.read())
    else:
        sys.exit(99)
elif name == "docker":
    a = list(args)
    if a[:1] in (["--host"], ["--context"]):
        if state.get("unreachable"):
            sys.exit(1)
        a = a[2:]
    if a == ["--version"]:
        print("Docker version test")
    elif a == ["info"]:
        pass
    elif a == ["info", "--format", "{{.OSType}}"]:
        print(state.get("remote_os", "linux"))
    elif a == ["buildx", "version"]:
        plugin = pathlib.Path(os.environ["DOCKER_CONFIG"]) / "cli-plugins/docker-buildx"
        sys.exit(0 if state.get("plugin_available", True) or plugin.is_file() else 1)
    elif a[:2] == ["buildx", "inspect"]:
        rest = [x for x in a[2:] if x != "--bootstrap"]
        inspect(rest[0] if rest else state.get("selected", "runner-remote"))
    elif a[:2] == ["buildx", "create"]:
        builder = a[a.index("--name") + 1]
        assert builder not in state.get("builders", {}), "duplicate builder creation"
        state.setdefault("builders", {})[builder] = {
            "driver": a[a.index("--driver") + 1], "endpoint": a[-1]
        }
        save()
    elif a[:2] == ["buildx", "use"]:
        state["selected"] = a[-1]
        save()
    elif a == ["buildx", "ls"]:
        print("NAME/NODE DRIVER/ENDPOINT STATUS")
        print(state.get("selected", "runner-remote") + "* docker-container")
    elif a[:2] == ["buildx", "du"]:
        # No du_total recorded behaves like an unavailable builder (exit 1), so
        # status --json reports cache_total null; gc tests opt in via state.
        if "du_total" not in state:
            sys.exit(1)
        print("Reclaimable: 5GB")
        print("Total: " + state["du_total"])
    elif a == ["context", "show"]:
        print(state.get("context", "desktop-linux"))
    elif a == ["context", "inspect", "desktop-linux"]:
        sys.exit(0 if state.get("desktop_available", True) else 1)
    elif a == ["context", "use", "desktop-linux"]:
        state["context"] = "desktop-linux"
        save()
    elif a[:1] == ["inspect"]:
        container = state.get("containers", {}).get(a[-1])
        if container is None:
            sys.exit(1)
        print("true" if container.get("running") else "false")
    elif a[:1] == ["run"]:
        container = a[a.index("--name") + 1]
        assert container not in state.get("containers", {}), "duplicate container"
        state.setdefault("containers", {})[container] = {"running": True}
        save()
        print("fake-container-id")
    elif a[:1] == ["start"]:
        state["containers"][a[-1]]["running"] = True
        save()
    elif a[:1] == ["rm"]:
        state.get("containers", {}).pop(a[-1], None)
        save()
    elif a[:2] == ["volume", "rm"]:
        state.setdefault("volumes_removed", []).append(a[-1])
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

    def gc_config(self, builder="runner-remote"):
        return self.config / "buildkitd" / f"{builder}.toml"

    def test_gc_configure_writes_policy_applied_at_creation(self):
        self.run_helper("gc", "configure", "--builder", "runner-remote", "--keep-bytes", "30")
        cfg = self.gc_config().read_text()
        self.assertIn('reservedSpace = "10GB"', cfg)
        self.assertIn('maxUsedSpace = "50GB"', cfg)
        self.assertIn("keepBytes = 32212254720", cfg)
        self.assertIn("keepDuration = 172800", cfg)
        self.remote()
        create = next(c for c in self.calls() if c[:3] == ["docker", "buildx", "create"])
        self.assertIn("--config", create)
        self.assertIn(str(self.gc_config()), create)

    def test_gc_configure_existing_builder_never_recreates_it(self):
        self.write_state({"builders": {"runner-remote": {"driver": "docker-container", "endpoint": "ssh://ci@builder:2222"}}})
        result = self.run_helper("gc", "configure", "--builder", "runner-remote")
        self.assertIn("rm --keep-state", result.stderr)
        self.assertIn("cache are unchanged", result.stderr)
        self.assertFalse(any(c[:3] in [["docker", "buildx", "rm"], ["docker", "buildx", "create"]] for c in self.calls()))

    def test_gc_configure_rejects_nonpositive_sizes(self):
        self.run_helper("gc", "configure", "--builder", "runner-remote", "--keep-bytes", "0", success=False)
        self.assertFalse(self.gc_config().exists())

    def test_gc_status_reports_buildkit_disk_usage(self):
        self.remote()
        state = json.loads((self.root / "state.json").read_text())
        state["du_total"] = "8GB"
        self.write_state(state)
        result = self.run_helper("gc", "status")
        self.assertIn("Total: 8GB", result.stdout)
        self.assertIn("runner-remote", result.stderr)

    def test_gc_configure_colima_vm_writes_buildkitd_toml_inside_vm(self):
        self.write_state({"colima_running": True})
        result = self.run_helper("gc", "configure", "--colima-vm", "--max-used-space", "80")
        vm_config = (self.root / "vm_stdin.txt").read_text()
        self.assertIn('maxUsedSpace = "80GB"', vm_config)
        self.assertIn("[worker.oci]", vm_config)
        ssh_call = next(c for c in self.calls() if c[:2] == ["colima", "ssh"])
        self.assertIn("/etc/buildkit/buildkitd.toml", ssh_call[-1])
        self.assertIn("colima stop && colima start", result.stderr)

    def test_registry_mirror_setup_wires_container_buildkit_and_daemon(self):
        self.write_state({"colima_running": True})
        result = self.run_helper("registry-mirror", "setup", "--builder", "runner-colima")
        run = next(c for c in self.calls() if c[:2] == ["docker", "--context"] and "run" in c)
        self.assertIn("runner-registry-mirror", run)
        self.assertIn("127.0.0.1:5001:5000", run)
        self.assertIn("REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io", run)
        self.assertIn("registry:2", run)
        toml = self.gc_config("runner-colima").read_text()
        self.assertIn('[registry."docker.io"]', toml)
        self.assertIn('mirrors = ["127.0.0.1:5001"]', toml)
        daemon = json.loads((self.root / ".colima/docker/daemon.json").read_text())
        self.assertEqual(daemon["registry-mirrors"], ["http://127.0.0.1:5001"])
        self.assertIn("WORKFLOW-FACING NOTES", result.stderr)

    def test_registry_mirror_setup_reuses_running_container(self):
        self.write_state({"colima_running": True})
        self.run_helper("registry-mirror", "setup", "--builder", "runner-colima")
        result = self.run_helper("registry-mirror", "setup", "--builder", "runner-colima")
        self.assertIn("Reusing running mirror container", result.stderr)
        runs = [c for c in self.calls() if c[:1] == ["docker"] and "run" in c]
        self.assertEqual(len(runs), 1)

    def test_registry_mirror_rejects_public_bind(self):
        self.write_state({"colima_running": True})
        self.run_helper("registry-mirror", "setup", "--bind", "0.0.0.0", success=False)
        self.assertFalse(any(c[:1] == ["docker"] and "run" in c for c in self.calls()))

    def test_registry_mirror_teardown_removes_wiring_but_keeps_data(self):
        self.write_state({"colima_running": True})
        self.run_helper("registry-mirror", "setup", "--builder", "runner-colima")
        result = self.run_helper("registry-mirror", "teardown")
        self.assertIn(["docker", "--context", "colima", "rm", "-f", "runner-registry-mirror"], self.calls())
        self.assertFalse(any("volume" in c for c in self.calls()))
        self.assertIn("kept", result.stderr)
        self.assertNotIn('[registry."docker.io"]', self.gc_config("runner-colima").read_text())
        daemon = json.loads((self.root / ".colima/docker/daemon.json").read_text())
        self.assertNotIn("registry-mirrors", daemon)

    def test_registry_mirror_teardown_remove_data_deletes_volume(self):
        self.write_state({"colima_running": True})
        self.run_helper("registry-mirror", "setup", "--builder", "runner-colima")
        self.run_helper("registry-mirror", "teardown", "--remove-data")
        self.assertIn(["docker", "--context", "colima", "volume", "rm", "runner-registry-mirror-data"], self.calls())

    def test_registry_mirror_status_reports_endpoint_and_wiring(self):
        self.write_state({"colima_running": True})
        self.run_helper("registry-mirror", "setup", "--builder", "runner-colima")
        result = self.run_helper("registry-mirror", "status")
        self.assertIn("running", result.stderr)
        self.assertIn("http://127.0.0.1:5001", result.stderr)
        self.assertIn("buildkit wired: runner-colima", result.stderr)

    def test_mirrored_builder_is_created_with_host_network(self):
        self.write_state({"colima_running": True})
        self.run_helper("registry-mirror", "setup", "--builder", "runner-remote")
        self.remote()
        create = next(c for c in self.calls() if c[:3] == ["docker", "buildx", "create"])
        self.assertIn("--config", create)
        self.assertEqual(create[create.index("--driver-opt") + 1], "network=host")


if __name__ == "__main__":
    unittest.main()
