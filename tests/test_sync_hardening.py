import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SyncHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "scripts/simple-hyper-sync.sh").read_text(encoding="utf-8")
        cls.service = (ROOT / "systemd/simple-hyper-sync.service").read_text(encoding="utf-8")
        cls.web_service = (ROOT / "systemd/simple-hyper.service").read_text(encoding="utf-8")

    def test_local_service_environment_is_preserved(self) -> None:
        self.assertIn("--exclude 'simple-hyper.env'", self.script)
        self.assertIn("--exclude '.env'", self.script)

    def test_etag_is_written_only_after_successful_restart(self) -> None:
        restart = self.script.index('systemctl restart "$SERVICE"')
        etag_write = self.script.index("printf '%s\\n' \"$target_etag\" > \"$STATE_FILE\"")
        self.assertLess(restart, etag_write)

    def test_remote_dependencies_are_not_installed_as_root(self) -> None:
        self.assertIn('runuser -u simplehyper -- "$PROJECT_DIR/.venv/bin/python" -m pip install', self.script)
        self.assertNotIn('  "$PROJECT_DIR/.venv/bin/python" -m pip install', self.script)

    def test_remote_sync_script_cannot_replace_root_entrypoint(self) -> None:
        self.assertNotIn("/usr/local/sbin/simple-hyper-sync.sh", self.script)

    def test_service_secures_root_executed_tree_before_script(self) -> None:
        exec_start = self.service.index("ExecStart=/usr/local/sbin/simple-hyper-sync.sh")
        chown = self.service.index("ExecStartPre=/usr/bin/chown -R root:root /opt/simple-hyper")
        chmod = self.service.index("ExecStartPre=/usr/bin/chmod -R go-w /opt/simple-hyper")
        self.assertLess(chown, exec_start)
        self.assertLess(chmod, exec_start)
        self.assertNotIn("ExecStart=/opt/simple-hyper/", self.service)

    def test_only_runtime_state_is_given_to_service_user(self) -> None:
        self.assertNotIn('chown simplehyper:simplehyper "$PROJECT_DIR"', self.script)
        self.assertIn('chown -R simplehyper:simplehyper "$RUNTIME_DIR"', self.script)

    def test_systemd_prepares_state_directories(self) -> None:
        self.assertIn("StateDirectory=simple-hyper simple-hyper-sync", self.service)
        self.assertIn("StateDirectory=simple-hyper", self.web_service)
        self.assertIn("EnvironmentFile=-/opt/simple-hyper/simple-hyper.env", self.web_service)


if __name__ == "__main__":
    unittest.main()
