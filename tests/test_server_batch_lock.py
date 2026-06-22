import multiprocessing
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path

from hl_order import args_need_server_batch_lock, server_batch_lock


def hold_batch_lock(path_text: str, ready) -> None:
    with server_batch_lock(Path(path_text)):
        ready.set()
        time.sleep(0.5)


class ServerBatchLockTests(unittest.TestCase):
    def test_nonblocking_worker_lock_skips_while_command_holds_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "server_batch.lock"
            ready = multiprocessing.Event()
            process = multiprocessing.Process(target=hold_batch_lock, args=(str(lock_path), ready))
            process.start()
            try:
                self.assertTrue(ready.wait(2))
                with server_batch_lock(lock_path, blocking=False) as acquired:
                    self.assertFalse(acquired)
            finally:
                process.join(3)
                if process.is_alive():
                    process.terminate()
                    process.join()
            self.assertEqual(process.exitcode, 0)
            with server_batch_lock(lock_path, blocking=False) as acquired:
                self.assertTrue(acquired)

    def test_mutating_batch_commands_require_lock(self) -> None:
        cases = (
            (Namespace(query=False, grid=True, trail=None, cancel=None), True),
            (Namespace(query=False, grid=False, trail="2%", cancel=None), True),
            (Namespace(query=False, grid=False, trail=None, cancel="grid"), True),
            (Namespace(query=False, grid=False, trail=None, cancel="all"), True),
            (Namespace(query=True, grid=True, trail=None, cancel=None), False),
            (Namespace(query=False, grid=False, trail=None, cancel=None), False),
        )
        for args, expected in cases:
            self.assertEqual(args_need_server_batch_lock(args), expected)


if __name__ == "__main__":
    unittest.main()
