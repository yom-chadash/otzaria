"""Real subprocess contracts for the fork-only Apple measurement owner."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import internal_apple_run as producer


class AppleProcessOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.log = self.root / "command.log"

    def tearDown(self):
        self.directory.cleanup()

    def receipt(self):
        return json.loads(self.log.with_suffix(".process.json").read_text())

    def test_completed_command_is_waited_and_log_is_retained(self):
        output = producer.run([sys.executable, "-c", "print('finished')"], self.root, self.log, timeout=5)
        self.assertEqual(output.strip(), "finished")
        receipt = self.receipt()
        self.assertTrue(receipt["commandCompleted"])
        self.assertTrue(receipt["ownedProcessGroupReaped"])
        self.assertFalse(receipt["timedOut"])
        self.assertEqual(receipt["exitCode"], 0)

    def test_nonzero_command_keeps_its_failure_and_log(self):
        with self.assertRaisesRegex(ValueError, "Command exited 7"):
            producer.run([sys.executable, "-c", "print('failed',flush=True);raise SystemExit(7)"],
                         self.root, self.log, timeout=5)
        self.assertIn("failed", self.log.read_text())
        self.assertEqual(self.receipt()["exitCode"], 7)
        self.assertTrue(self.receipt()["ownedProcessGroupReaped"])

    def test_timeout_closes_owned_descendant_and_keeps_unrelated_process(self):
        control = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
        program = ("import subprocess,sys,time;"
                   "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                   "print(child.pid,flush=True);print('phase-open',flush=True);time.sleep(30)")
        try:
            with self.assertRaises(TimeoutError):
                producer.run([sys.executable, "-c", program], self.root, self.log, timeout=.3)
            self.assertIsNone(control.poll())
            lines = self.log.read_text().splitlines()
            self.assertEqual(lines[1], "phase-open")
            state = subprocess.run(["ps", "-o", "stat=", "-p", lines[0]],
                                   text=True, capture_output=True, timeout=5).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)
            receipt = self.receipt()
            self.assertTrue(receipt["timedOut"])
            self.assertTrue(receipt["ownedProcessGroupReaped"])
            self.assertFalse(receipt["commandCompleted"])
        finally:
            control.terminate()
            control.wait(timeout=5)

    def test_cleanup_failure_does_not_claim_reap_or_propagate_plain_timeout(self):
        owned = []
        cleanup = producer.cleanup_group

        def refuse(process):
            owned.append(process)
            raise PermissionError("test cleanup refusal")

        try:
            with patch.object(producer, "cleanup_group", side_effect=refuse):
                with self.assertRaisesRegex(PermissionError, "cleanup refusal"):
                    producer.run([sys.executable, "-c", "import time;time.sleep(30)"],
                                 self.root, self.log, timeout=.1)
            receipt = self.receipt()
            self.assertTrue(receipt["timedOut"])
            self.assertFalse(receipt["ownedProcessGroupReaped"])
            self.assertFalse(receipt["commandCompleted"])
        finally:
            for process in owned:
                cleanup(process)


if __name__ == "__main__":
    unittest.main()
