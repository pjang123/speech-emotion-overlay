"""Link fetching: SSRF guards with real DNS rules, downloads against a local HTTP server.

The local server is on 127.0.0.1, which the real guard (correctly) refuses, so the
download tests pass a permissive host check; the guard itself is tested separately.
"""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import make_image
from emotion_overlay.fetch import FetchError, check_public_host, check_url, fetch_image, page_image

GIF = make_image("GIF")


def allow_all(host: str) -> None:
    return None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a.gif",
        "http://user:pass@example.com/a.gif",
        "http://example.com:22/a.gif",
        "not a url",
    ],
)
def test_rejected_urls(url: str) -> None:
    with pytest.raises(FetchError):
        check_url(url, allow_all)


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0"],  # noqa: S104, E501 - these must be refused
)
def test_private_and_local_addresses_are_refused(host: str) -> None:
    with pytest.raises(FetchError, match="local"):
        check_public_host(host)


def test_page_image_finds_og_image() -> None:
    html = """<html><head>
      <meta name="twitter:image" content="https://cdn.example/t.gif">
      <meta content="/media/x.gif" property="og:image">
    </head></html>"""
    assert page_image(html, "https://tenor.example/view/1") == "https://tenor.example/media/x.gif"
    assert page_image("<html></html>", "https://a.example/") is None


def test_page_image_prefers_gif_over_webp() -> None:
    # Giphy lists an animated WebP first and the GIF second.
    html = """<meta property="og:image" content="https://m.example/a/giphy.webp"/>
              <meta property="og:image" content="https://m.example/a/giphy.gif?cid=1"/>"""
    assert page_image(html, "https://giphy.example/") == "https://m.example/a/giphy.gif?cid=1"


class Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802
        status, ctype, body = self.routes.get(self.path, (404, "text/plain", b"nope"))
        self.send_response(status)
        if status in (301, 302):
            self.send_header("Location", body.decode())
            body = b""
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    Handler.routes = {
        "/cat.gif": (200, "image/gif", GIF),
        "/page": (
            200,
            "text/html",
            f'<meta property="og:image" content="{base}/cat.gif">'.encode(),
        ),
        "/page-of-page": (
            200,
            "text/html",
            f'<meta property="og:image" content="{base}/page">'.encode(),
        ),
        "/empty-page": (200, "text/html", b"<html>no image</html>"),
        "/big.gif": (200, "image/gif", b"G" * 5000),
        "/moved": (302, "", f"{base}/cat.gif".encode()),
    }
    yield base
    httpd.shutdown()


def test_direct_gif_link(server: str) -> None:
    data, name = fetch_image(f"{server}/cat.gif", 10_000, check_host=allow_all)
    assert data == GIF and name == "cat.gif"


def test_page_link_follows_og_image_once(server: str) -> None:
    assert fetch_image(f"{server}/page", 10_000, check_host=allow_all)[0] == GIF
    with pytest.raises(FetchError, match="web page"):
        fetch_image(f"{server}/page-of-page", 10_000, check_host=allow_all)
    with pytest.raises(FetchError, match="no image"):
        fetch_image(f"{server}/empty-page", 10_000, check_host=allow_all)


def test_size_limit_by_bytes_read(server: str) -> None:
    with pytest.raises(FetchError, match="larger"):
        fetch_image(f"{server}/big.gif", 1000, check_host=allow_all)


def test_http_errors_are_reported(server: str) -> None:
    with pytest.raises(FetchError, match="404"):
        fetch_image(f"{server}/missing.gif", 10_000, check_host=allow_all)


def test_redirects_are_rechecked(server: str) -> None:
    assert fetch_image(f"{server}/moved", 10_000, check_host=allow_all)[0] == GIF
    seen: list[str] = []

    def only_first_hop(host: str) -> None:
        seen.append(host)
        if len(seen) > 1:
            raise FetchError("links to this computer or a local network aren't allowed")

    with pytest.raises(FetchError, match="local"):
        fetch_image(f"{server}/moved", 10_000, check_host=only_first_hop)


def test_real_guard_blocks_the_local_server(server: str) -> None:
    with pytest.raises(FetchError, match="local"):
        fetch_image(f"{server}/cat.gif", 10_000)
