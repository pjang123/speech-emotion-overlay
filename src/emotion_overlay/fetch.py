"""Download an image (usually a GIF) from a link the user pasted.

This is the only place the app contacts the internet on the user's behalf, and
only when they explicitly add an image from a link.

Making a server fetch a URL is a classic SSRF risk, so:
- only http/https, no credentials in the URL, no non-default ports below 1024
  other than 80/443;
- the hostname must resolve to public addresses only: links to this PC, the
  local network, link-local/cloud-metadata addresses etc. are refused, and the
  check is repeated for every redirect;
- the response is capped by bytes actually read, and the result still goes
  through the normal image validation (Pillow, 4 formats) before it's stored.

Tenor/Giphy *page* links are HTML, not images; for those, the page's `og:image`
(the GIF itself) is followed, once.

Known limit: the address check and the connection resolve the name separately,
so a DNS server answering differently within milliseconds could slip past it.
"""

import ipaddress
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_S = 10
USER_AGENT = "EmotionOverlay/0.1 (+local overlay app; fetches one image on user request)"


class FetchError(ValueError):
    pass


def check_public_host(host: str) -> None:
    """Refuse hosts that resolve to anything but public internet addresses."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"couldn't find the website '{host}'") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise FetchError("links to this computer or a local network aren't allowed")


HostCheck = Callable[[str], None]


def check_url(url: str, check_host: HostCheck) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise FetchError("only http:// and https:// links are supported")
    if not parts.hostname:
        raise FetchError("that doesn't look like a web link")
    if parts.username or parts.password:
        raise FetchError("links with a username or password aren't allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise FetchError("invalid port in link") from exc
    if port is not None and port < 1024 and port not in (80, 443):
        raise FetchError("that port isn't allowed")
    check_host(parts.hostname)
    return parts.geturl()


IMAGE_META = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image")


class _MetaImage(HTMLParser):
    """Collects og:image / twitter:image candidates from a page, in page order."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        a = {k.lower(): v for k, v in attrs if v is not None}
        key = (a.get("property") or a.get("name") or "").lower()
        if key in IMAGE_META and a.get("content"):
            self.found.append((key, a["content"]))


def page_image(html: str, base_url: str) -> str | None:
    """The page's preview image, preferring a GIF (Giphy lists a WebP first)."""
    parser = _MetaImage()
    parser.feed(html)
    ranked = sorted(
        parser.found,
        key=lambda item: (
            not urlsplit(item[1]).path.lower().endswith(".gif"),
            IMAGE_META.index(item[0]),
        ),
    )
    return urljoin(base_url, ranked[0][1]) if ranked else None


def _opener(check_host: HostCheck) -> urllib.request.OpenerDirector:
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        max_redirections = MAX_REDIRECTS

        def redirect_request(
            self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
        ) -> Any:  # noqa: E501
            check_url(newurl, check_host)  # every hop must pass the same checks
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    # No proxy handler: system proxy settings shouldn't silently reroute this.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect())


def fetch_image(
    url: str, max_bytes: int, check_host: HostCheck = check_public_host, _follow_page: bool = True
) -> tuple[bytes, str]:
    """Return (bytes, suggested filename). The bytes are NOT validated as an image here."""
    safe_url = check_url(url, check_host)
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        safe_url,
        headers={
            "User-Agent": USER_AGENT,
            # Once we're following a page's image link, ask for the image only:
            # Tenor's media server sends an HTML viewer to anyone who accepts HTML.
            "Accept": "image/*,text/html;q=0.8" if _follow_page else "image/*",
        },
    )
    try:
        with _opener(check_host).open(request, timeout=TIMEOUT_S) as response:
            final_url = response.geturl()
            content_type = (response.headers.get("Content-Type") or "").lower()
            is_html = content_type.startswith("text/html")
            limit = MAX_HTML_BYTES if is_html else max_bytes
            data = response.read(limit + 1)
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise FetchError(f"the website answered {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, FetchError):
            raise reason from exc
        raise FetchError(f"couldn't download the link ({reason})") from exc

    if len(data) > limit:
        raise FetchError(f"the file is larger than {limit // (1024 * 1024)} MB")
    if is_html:
        if not _follow_page:
            raise FetchError("that link is a web page, not an image")
        found = page_image(data.decode("utf-8", errors="replace"), final_url)
        if not found:
            raise FetchError("that page has no image to take; try 'Copy image address' instead")
        image, file_name = fetch_image(found, max_bytes, check_host, _follow_page=False)
        # The page link usually says what the GIF is; the media file is "giphy.gif".
        return image, name_from_page(final_url, file_name)

    name = unquote(PurePosixPath(urlsplit(final_url).path).name) or "image"
    return data, name


_TENOR_ID = re.compile(r"-gif-\d+$")
# Giphy ids are 10+ characters with capitals or digits, so a long final word like
# "celebration" isn't mistaken for one.
_GIPHY_ID = re.compile(r"(^|-)(?=[a-z]*[A-Z0-9])[A-Za-z0-9]{10,}$")


def name_from_page(page_url: str, file_name: str) -> str:
    """'tenor.com/view/happy-dance-gif-123' -> 'happy dance.gif';
    'giphy.com/gifs/thank-you-BYoRqTmcgzHcL9TCy1' -> 'thank you.gif'."""
    slug = unquote(PurePosixPath(urlsplit(page_url).path).name)
    slug = _GIPHY_ID.sub("", _TENOR_ID.sub("", slug))
    words = ""
    for word in slug.replace("-", " ").replace("_", " ").split():
        if len(words) + len(word) + 1 > 60:  # keep whole words; slugs can be very long
            break
        words = f"{words} {word}".strip()
    if not re.search(r"[A-Za-z]", words):
        return file_name
    extension = PurePosixPath(file_name).suffix or ".gif"
    return f"{words}{extension}"
