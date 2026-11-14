import json
import os
import signal
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
POD = os.environ.get("HOSTNAME") or socket.gethostname()


def read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read().strip()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":
            healthy = read("health.txt") == "ok"
            self.reply(200 if healthy else 503, "ok\n" if healthy else "unhealthy\n", "text/plain")
            return
        if url.path == "/":
            status = self.reply(200, read("body.txt") + "\n", "text/plain")
        elif url.path == "/pod":
            status = self.reply(200, POD + "\n", "text/plain")
        elif url.path == "/env":
            status = self.reply(200, json.dumps(dict(os.environ), sort_keys=True) + "\n", "application/json")
        else:
            status = self.reply(404, "not found\n", "text/plain")
        marker = parse_qs(url.query).get("marker", ["-"])[0]
        print("request path=%s status=%d marker=%s pod=%s" % (url.path, status, marker, POD), flush=True)

    def reply(self, status, body, content_type):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return status

    def log_message(self, *args):
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("listening on :%d pod=%s greeting=%s" % (port, POD, os.environ.get("GREETING", "")), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
