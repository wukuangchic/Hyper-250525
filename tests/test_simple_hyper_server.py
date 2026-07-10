import io
import socket
import threading
import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

import simple_hyper_server as server


class BodyHandler:
    def __init__(self, content_length: str, body: bytes = b"") -> None:
        self.headers = {"Content-Length": content_length}
        self.rfile = io.BytesIO(body)


class PostHandler(BodyHandler):
    path = "/api/run"

    def __init__(self, content_length: str, body: bytes = b"") -> None:
        super().__init__(content_length, body)
        self.response = None

    def send_json(self, payload, status=HTTPStatus.OK) -> None:
        self.response = (payload, status)


class SimpleHyperServerTests(unittest.TestCase):
    def test_negative_content_length_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid content length"):
            server.parse_json_body(BodyHandler("-1"))

    def test_body_read_timeout_returns_request_timeout(self) -> None:
        handler = PostHandler("2")
        handler.rfile = SimpleNamespace(read=lambda _length: (_ for _ in ()).throw(socket.timeout()))
        server.SimpleHyperHandler.do_POST(handler)
        self.assertEqual(handler.response[1], HTTPStatus.REQUEST_TIMEOUT)
        self.assertEqual(handler.response[0]["error"], "request body read timed out")

    def test_handler_setup_sets_connection_read_timeout(self) -> None:
        handler = server.SimpleHyperHandler.__new__(server.SimpleHyperHandler)
        handler.connection = SimpleNamespace(settimeout=lambda value: setattr(handler, "timeout", value))
        with patch("http.server.BaseHTTPRequestHandler.setup"):
            server.SimpleHyperHandler.setup(handler)
        self.assertEqual(handler.timeout, server.CONNECTION_READ_TIMEOUT)

    def test_command_capacity_is_global_and_fails_fast(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_run(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return SimpleNamespace(returncode=0, stdout="ok")

        semaphore = threading.BoundedSemaphore(1)
        result = []
        with patch.object(server, "COMMAND_SLOTS", semaphore), patch.object(server.subprocess, "run", blocked_run):
            worker = threading.Thread(target=lambda: result.append(server.run_hl_order(["query"], "address", "key")))
            worker.start()
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaisesRegex(server.CommandCapacityError, "capacity is full"):
                    server.run_hl_order(["query"], "address", "key")
            finally:
                release.set()
                worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["output"], "ok")

    def test_capacity_error_returns_service_unavailable(self) -> None:
        body = b'{"command":"query"}'
        handler = PostHandler(str(len(body)), body)
        credentials = ("0x" + "1" * 40, "0x" + "2" * 64)
        with patch.object(server, "load_server_credentials", return_value=credentials), patch.object(
            server, "run_hl_order", side_effect=server.CommandCapacityError("command capacity is full; retry later")
        ):
            server.SimpleHyperHandler.do_POST(handler)

        self.assertEqual(handler.response[1], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(handler.response[0]["error"], "command capacity is full; retry later")

    def test_connection_capacity_closes_excess_request_without_thread(self) -> None:
        http_server = server.BoundedThreadingHTTPServer.__new__(server.BoundedThreadingHTTPServer)
        http_server.connection_slots = threading.BoundedSemaphore(1)
        self.assertTrue(http_server.connection_slots.acquire(blocking=False))
        request = SimpleNamespace()

        with patch.object(http_server, "shutdown_request") as shutdown:
            http_server.process_request(request, ("127.0.0.1", 1))

        shutdown.assert_called_once_with(request)


if __name__ == "__main__":
    unittest.main()
