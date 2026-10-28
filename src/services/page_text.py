"""Turn a fetched web page into text an agent can read: `fetch_page`'s conversion.

Pure functions over bytes already in memory — no network, no vault. The fetch
itself, and every outbound safeguard, is `transfer.fetch_url_guarded`.

Three output formats, named in the result so the caller knows what it got:

- `markdown` — an HTML page converted with markdownify, after cutting it down
  to its main content. The default for HTML.
- `text` — any other text type (plain text, JSON, XML, CSV), decoded and
  otherwise unchanged. There is nothing to convert.
- `raw` — the decoded body exactly as served, HTML included. The caller's
  fallback when the conversion dropped something it needed.

**Main-content trimming.** Most of an HTML page is not its content: menus,
sidebars, footers, scripts. When the page marks its content with `<main>`,
`role="main"` (older sites, Sphinx documentation) or `<article>`, only that
element is kept; otherwise the body is kept minus the
page-chrome elements (`<nav>`, `<header>`, `<footer>`, `<aside>`). `<header>`
and `<footer>` are only dropped in the second case: inside an article they
usually hold its title and byline (Wikipedia's `<h1>` sits in a `<header>`
inside `<main>`).

**Links are dropped by default.** On an encyclopedia article the link targets
are two thirds of the converted text, so the default keeps the link text and
drops the URL. `keep_links=True` keeps them, made absolute against the page's
final URL so an agent can pass one straight back to `fetch_page`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, UnicodeDammit
from markdownify import MarkdownConverter

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# Non-`text/*` types that are still text. `+json` and `+xml` suffixes are
# matched separately, in `is_text_type`.
_TEXT_APPLICATION_TYPES = frozenset({
    "application/json", "application/xml", "application/javascript",
    "application/ecmascript", "application/x-yaml", "application/yaml",
})

# Never content, wherever they sit. Images go too: an agent reading text has
# no use for an image reference, and alt text is usually a filename.
_ALWAYS_DROP = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "object", "embed", "form", "button", "img", "picture", "video", "audio",
    "source", "track", "map", "nav", "aside",
)
# Page chrome, dropped only when the page has no `<main>` or `<article>` —
# see the module docstring for why not inside one.
_PAGE_CHROME = ("header", "footer")

_LINK_SCHEMES = frozenset({"http", "https"})
_BLANK_RUN = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class PageText:
    format: str  # "markdown" | "text" | "raw"
    content: str
    title: str | None
    # True when the body was not valid in the encoding it was decoded with and
    # some bytes became U+FFFD. Reported, never hidden: the text is still
    # returned, but the caller must know it is not exactly what was served.
    lossy_decoding: bool


def media_type(content_type: str | None) -> str | None:
    """The bare, lower-cased media type of a `Content-Type` header."""
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def is_text_type(mtype: str | None) -> bool:
    """Whether `fetch_page` can return a body of this type as text."""
    if mtype is None:
        return False
    if mtype.startswith("text/") or mtype in HTML_TYPES or mtype in _TEXT_APPLICATION_TYPES:
        return True
    return mtype.startswith("application/") and mtype.endswith(("+json", "+xml"))


def _declared_charset(content_type: str | None) -> str | None:
    if not content_type:
        return None
    for param in content_type.split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"').strip("'")
    return None


def decode(body: bytes, content_type: str | None) -> tuple[str, bool]:
    """Decode a body: the declared charset if any, else what the bytes say.

    A charset in the `Content-Type` header is honoured even when some bytes
    are invalid in it: those bytes are replaced and reported. Letting a
    detector pick another charset instead would turn a UTF-8 page with one
    bad byte into cp1252 mojibake from end to end. Without a declaration,
    `UnicodeDammit` reads a `<meta charset>` (HTML) or guesses. Returns the
    text and whether any bytes had to be replaced.
    """
    declared = _declared_charset(content_type)
    if declared:
        try:
            return body.decode(declared), False
        except UnicodeDecodeError:
            return body.decode(declared, errors="replace"), True
        except LookupError:
            pass  # a charset Python does not know: detect instead, below
    dammit = UnicodeDammit(body, is_html=media_type(content_type) in HTML_TYPES)
    if dammit.unicode_markup is None:
        # Nothing decoded cleanly, not even with replacement. UTF-8 with
        # replacement still returns everything, and says so.
        return body.decode("utf-8", errors="replace"), True
    return dammit.unicode_markup, bool(dammit.contains_replacement_characters)


def _same_page(url: str, page_url: str) -> bool:
    return urldefrag(url).url == urldefrag(page_url).url


def html_to_markdown(html: str, *, page_url: str, keep_links: bool) -> tuple[str, str | None]:
    """Convert an HTML page to Markdown. Returns the Markdown and the `<title>`."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else None

    for tag in soup.find_all(_ALWAYS_DROP):
        tag.decompose()
    root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article")
    if root is None:
        root = soup.body or soup
        for tag in root.find_all(_PAGE_CHROME):
            tag.decompose()

    # `title` attributes are tooltips; markdownify renders a link's as a quoted
    # suffix that repeats the link text.
    for tag in root.find_all(title=True):
        del tag["title"]

    for anchor in root.find_all("a"):
        href = anchor.get("href")
        target = urljoin(page_url, href) if href else None
        if (
            not keep_links
            or target is None
            or urlsplit(target).scheme not in _LINK_SCHEMES
            # In-page anchors (Wikipedia's citation markers are hundreds of
            # them) point back at the page the caller already has.
            or _same_page(target, page_url)
        ):
            anchor.unwrap()
        else:
            anchor["href"] = target

    markdown = MarkdownConverter(heading_style="ATX").convert_soup(root)
    return _BLANK_RUN.sub("\n\n", markdown).strip(), title


def page_text(
    body: bytes, content_type: str | None, *, page_url: str, keep_links: bool, raw: bool
) -> PageText:
    """The text `fetch_page` returns for a body it accepted."""
    text, lossy = decode(body, content_type)
    if raw:
        return PageText("raw", text, None, lossy)
    if media_type(content_type) in HTML_TYPES:
        markdown, title = html_to_markdown(text, page_url=page_url, keep_links=keep_links)
        return PageText("markdown", markdown, title, lossy)
    return PageText("text", text, None, lossy)
