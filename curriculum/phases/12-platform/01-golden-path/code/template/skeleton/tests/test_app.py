import json
import threading
import unittest
import urllib.request

from app.main import SERVICE, serve


class AppTest(unittest.TestCase):
    def setUp(self):
        self.server = serve(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, json.load(response)

    def test_healthz(self):
        self.assertEqual(self.get("/healthz"), (200, {"status": "ok"}))

    def test_other_paths_name_the_service(self):
        self.assertEqual(self.get("/orders"), (200, {"service": SERVICE, "path": "/orders"}))


if __name__ == "__main__":
    unittest.main()
