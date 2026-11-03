"""Loopback-only demonstration server, Python standard library."""
import argparse
import json
import secrets
import tempfile
from http import cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from src.demo import run_scenario
from src.publication import PublicationService, DomainError
from tools.fixtures import sample_property

ROOT = Path(__file__).resolve().parents[1]


def create_server(port=8765, db_path=None):
    token = secrets.token_urlsafe(32)
    cookie = secrets.token_urlsafe(32)
    db_path = db_path or ROOT / "runtime" / "gp.sqlite"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with PublicationServiceContext(db_path) as service:
        try:
            service.get_property(sample_property()["id"])
        except DomainError:
            service.seed(sample_property())

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "dist"), **kwargs)

        def log_message(self, fmt, *args):
            # Avoid logging cookies, payloads, tokens or personal data.
            return

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            super().end_headers()

        def json_response(self, data, status=200, session=False):
            payload = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            if session:
                self.send_header("Set-Cookie", f"gp_demo={cookie}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self, write=False):
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                return False
            if "gp_demo" not in jar or not secrets.compare_digest(jar["gp_demo"].value, cookie):
                return False
            if write and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), token):
                return False
            origin = self.headers.get("Origin")
            return not origin or origin in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")

        def valid_host(self):
            return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.valid_host():
                return self.json_response({"error": "INVALID_HOST"}, 403)
            path = urlparse(self.path).path
            if path == "/api/session":
                return self.json_response({"csrf": token, "mode": "local-demo"}, session=True)
            if path.startswith("/api/"):
                if not self.authorized():
                    return self.json_response({"error": "UNAUTHORIZED"}, 401)
                try:
                    with PublicationServiceContext(db_path) as service:
                        if path.startswith("/api/properties/"):
                            return self.json_response(service.get_property(path.rsplit("/", 1)[1]))
                        if path.startswith("/api/publications/"):
                            return self.json_response(service.get_job(path.rsplit("/", 1)[1]))
                except DomainError as exc:
                    return self.json_response(exc.as_dict(), exc.http_status)
                return self.json_response({"error": "NOT_FOUND"}, 404)
            return super().do_GET()

        def do_POST(self):
            self.mutate("POST")

        def do_PATCH(self):
            self.mutate("PATCH")

        def mutate(self, method):
            if not self.valid_host() or not self.authorized(write=True):
                return self.json_response({"error": "AUTH_OR_CSRF_FAILED"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 65536:
                    return self.json_response({"error": "BODY_SIZE"}, 413)
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    return self.json_response({"error": "EXPECTED_OBJECT"}, 400)
                path = urlparse(self.path).path
                if method == "POST" and path == "/api/demo":
                    with tempfile.TemporaryDirectory() as temp:
                        return self.json_response(run_scenario(body.get("scenario", "success"), Path(temp) / "scenario.sqlite"))
                with PublicationServiceContext(db_path) as service:
                    if method == "PATCH" and path.startswith("/api/properties/"):
                        return self.json_response(service.patch_property(path.rsplit("/", 1)[1], body.get("expectedVersion"), body.get("changes", {})))
                    if method == "POST" and path == "/api/publications":
                        result, created = service.enqueue(body["propertyId"], body.get("expectedVersion"))
                        return self.json_response(result, 202 if created else 200)
                return self.json_response({"error": "NOT_FOUND"}, 404)
            except DomainError as exc:
                return self.json_response(exc.as_dict(), exc.http_status)
            except (ValueError, KeyError, TypeError):
                return self.json_response({"error": "BAD_REQUEST"}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


class PublicationServiceContext(PublicationService):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.port)
    print(f"GP / Fieldnotes: http://127.0.0.1:{server.server_port}", flush=True)
    print("Local educational data only. Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
