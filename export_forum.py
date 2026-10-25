#!/usr/bin/env python3
"""Create a browsable, paginated static HTML archive from an SMF 2.1 database."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit

try:
    import mysql.connector
except ImportError:
    mysql = None
else:
    mysql = mysql.connector


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
BBCODE_TOKEN_RE = re.compile(
    r"\[(?P<close>/)?(?P<tag>[a-z]+|[*+@#]|[xo0])(?:(?:[ \t]*=[ \t]*|[ \t]+)(?P<arg>[^\]]*))?\]",
    re.I,
)
ATTACHMENT_RE = re.compile(r"\[attach(?:[ \t]+(?P<args>[^\]]*))?\](?P<body>.*?)\[/attach\]", re.I | re.S)
LINK_RE = re.compile(r"\[(?P<tag>url|iurl|ftp)(?:=([^\]]+))?\](.*?)\[/\1\]", re.I | re.S)
EMAIL_RE = re.compile(r"\[email(?:=([^\]]+))?\](.*?)\[/email\]", re.I | re.S)
CODE_RE = re.compile(r"\[code(?:=[^\]]*)?\](.*?)\[/code\]", re.I | re.S)
IMAGE_RE = re.compile(r"\[img(?:[ \t]+(?P<args>[^\]]*))?\](?P<url>.*?)\[/img\]", re.I | re.S)
UNPARSED_RE = re.compile(r"\[(?P<tag>html|nobbc|php)(?:[^\]]*)\](?P<body>.*?)\[/\1\]", re.I | re.S)
YOUTUBE_RE = re.compile(r"\[youtube\]([A-Za-z0-9_-]{6,20})\[/youtube\]", re.I)
FLASH_RE = re.compile(r"\[flash=\d{1,4},\d{1,4}\](.*?)\[/flash\]", re.I | re.S)
SIZE_VALUE_RE = re.compile(
    r"(?:\d{1,3}(?:\.\d+)?(?:px|pt|em|rem|%)|xx-small|x-small|small|medium|large|x-large|xx-large|xxx-large|smaller|larger)",
    re.I,
)
ATTACHMENT_NAME_RE = re.compile(r"[\x00-\x1f\x7f/\\]+")
NO_TOPICS_ROW = '<tr><td colspan="4">No public topics in this forum.</td></tr>'


class ExportError(Exception):
    """An expected export configuration or data error."""


@dataclass(frozen=True)
class Board:
    board_id: int
    category_id: int
    parent_id: int
    name: str
    description: str


def escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def escape_text(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def escape_forum_text(value: Any) -> str:
    return escape_text(html.unescape(str(value or "")))


def attachment_filename(value: Any) -> str:
    filename = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    filename = ATTACHMENT_NAME_RE.sub("_", filename).strip(" .")
    return filename or "attachment"


def relative_url(path: str) -> str:
    return quote(path, safe="/-._~")


def safe_url(value: str) -> str | None:
    value = html.unescape(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if (scheme in {"http", "https", "ftp", "ftps", "sftp"} and parsed.netloc) or (scheme == "mailto" and parsed.path):
        return value
    return None


EXTERNAL_LINK_ATTRIBUTES = ' target="_blank" rel="nofollow noopener noreferrer"'


def format_post(
    body: str,
    attachments: dict[int, dict[str, str]] | None = None,
    image_resolver: Callable[[str], str | None] | None = None,
    smileys: dict[str, str] | None = None,
) -> str:
    """Render supported SMF BBCode as safe static HTML."""
    body = html.unescape(body)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    attachments = attachments or {}
    smileys = smileys or {}
    placeholders: dict[str, str] = {}

    def hold(fragment: str) -> str:
        key = f"\x00SAFE{len(placeholders)}\x00"
        placeholders[key] = fragment
        return key

    def code_block(match: re.Match[str]) -> str:
        return hold(f"<pre><code>{escape_text(match.group(1))}</code></pre>")

    body = CODE_RE.sub(code_block, body)

    def image(match: re.Match[str]) -> str:
        image_url = match.group("url").strip()
        source = image_resolver(image_url) if image_resolver else None
        external_source = safe_url(image_url)
        if source is None and external_source:
            parsed_source = urlsplit(external_source)
            if parsed_source.scheme.lower() in {"http", "https"}:
                source = external_source
        if source is None:
            return match.group(0)
        try:
            parameters = shlex.split(match.group("args") or "")
        except ValueError:
            return match.group(0)
        attributes = {"alt": "", "title": ""}
        dimensions: dict[str, str] = {}
        for parameter in parameters:
            key, separator, value = parameter.partition("=")
            key = key.lower()
            if not separator:
                continue
            if key in attributes:
                attributes[key] = value
            elif key in {"width", "height"} and re.fullmatch(r"\d{1,4}", value):
                dimensions[key] = value
        extra_attributes = "".join(f' {name}="{value}"' for name, value in dimensions.items())
        title_attribute = f' title="{escape(attributes["title"])}"' if attributes["title"] else ""
        return hold(
            f'<img class="post-image" src="{escape(source)}" alt="{escape(attributes["alt"])}"'
            f'{title_attribute}{extra_attributes}>'
        )

    body = IMAGE_RE.sub(image, body)

    def unparsed(match: re.Match[str]) -> str:
        content = escape_text(match.group("body"))
        if match.group("tag").lower() == "php":
            content = f'<span class="phpcode">{content}</span>'
        return hold(content)

    body = UNPARSED_RE.sub(unparsed, body)

    body = YOUTUBE_RE.sub(
        lambda match: hold(
            f'<a href="https://www.youtube.com/watch?v={escape(match.group(1))}"{EXTERNAL_LINK_ATTRIBUTES}>'
            f'https://www.youtube.com/watch?v={escape(match.group(1))}</a>'
        ),
        body,
    )
    def flash_link(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        destination = safe_url(target)
        if destination is None:
            return match.group(0)
        return hold(f'<a href="{escape(destination)}"{EXTERNAL_LINK_ATTRIBUTES}>{escape_text(target)}</a>')

    body = FLASH_RE.sub(flash_link, body)

    def attachment_link(match: re.Match[str]) -> str:
        arguments = match.group("args") or ""
        id_match = re.search(r"(?:^|\s)id=(\d+)(?:\s|$)", arguments, re.I)
        body_id = re.fullmatch(r"\s*(\d+)\s*", match.group("body"))
        attachment_id = int((id_match or body_id).group(1)) if id_match or body_id else 0
        attachment = attachments.get(attachment_id)
        if attachment is None:
            return match.group(0)
        return hold(
            f'<a href="{escape(relative_url(attachment["href"]))}" download="{escape(attachment["name"])}">'
            f'{escape(attachment["name"])}</a>'
        )

    body = ATTACHMENT_RE.sub(attachment_link, body)

    def link(match: re.Match[str]) -> str:
        tag = match.group("tag").lower()
        explicit_target = match.group(2)
        visible = match.group(3)
        target = explicit_target or visible
        if tag == "ftp" and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
            target = "ftp://" + target.lstrip("/")
        destination = safe_url(target)
        if destination is None:
            return escape(match.group(0))
        return hold(f'<a href="{escape(destination)}"{EXTERNAL_LINK_ATTRIBUTES}>{escape_text(visible)}</a>')

    body = LINK_RE.sub(link, body)

    def email_link(match: re.Match[str]) -> str:
        address = (match.group(1) or match.group(2)).strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            return escape_text(match.group(0))
        return hold(
            f'<a href="mailto:{escape(address)}" target="_blank" rel="noopener noreferrer">'
            f'{escape_text(address)}</a>'
        )

    body = EMAIL_RE.sub(email_link, body)

    wrappers = {
        "b": ("<strong>", "</strong>"),
        "i": ("<em>", "</em>"),
        "u": ("<u>", "</u>"),
        "s": ("<s>", "</s>"),
        "quote": ("<blockquote>", "</blockquote>"),
        "list": ("<ul>", "</ul>"),
        "li": ("<li>", "</li>"),
        "size": ("", "</span>"),
        "sub": ("<sub>", "</sub>"),
        "sup": ("<sup>", "</sup>"),
        "pre": ("<pre>", "</pre>"),
        "center": ('<div class="centertext">', "</div>"),
        "left": ('<div class="lefttext">', "</div>"),
        "right": ('<div class="righttext">', "</div>"),
        "justify": ('<div class="justifytext">', "</div>"),
        "ltr": ('<bdo dir="ltr">', "</bdo>"),
        "rtl": ('<bdo dir="rtl">', "</bdo>"),
        "tt": ('<span class="monospace">', "</span>"),
        "table": ('<table class="bbc_table">', "</table>"),
        "tr": ("<tr>", "</tr>"),
        "td": ("<td>", "</td>"),
        "time": ('<span class="bbc_time">', "</span>"),
        "bdo": ("", "</bdo>"),
        "abbr": ("", "</abbr>"),
        "acronym": ("", "</abbr>"),
        "anchor": ("", "</span>"),
        "font": ("", "</span>"),
        "color": ("", "</span>"),
        "black": ('<span style="color:black">', "</span>"),
        "blue": ('<span style="color:blue">', "</span>"),
        "green": ('<span style="color:green">', "</span>"),
        "red": ('<span style="color:red">', "</span>"),
        "white": ('<span style="color:white">', "</span>"),
        "me": ('<div class="meaction">* ', "</div>"),
        "member": ("@", ""),
        "br": ("<br>", ""),
        "hr": ("<hr>", ""),
        "move": ("<span>", "</span>"),
        "glow": ('<span class="bbc_glow">', "</span>"),
        "shadow": ('<span class="bbc_shadow">', "</span>"),
        "float": ("", "</div>"),
    }
    color_value_re = re.compile(
        r"(?:#[\da-f]{3}(?:[\da-f]{3})?|[a-z]{1,20}|rgb\(\s*(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*,\s*(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*,\s*(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*\))",
        re.I,
    )
    list_type_re = re.compile(
        r"(?:none|disc|circle|square|decimal|decimal-leading-zero|lower-roman|upper-roman|lower-alpha|upper-alpha|lower-greek|upper-greek|lower-latin|upper-latin|hebrew|armenian|georgian|cjk-ideographic|hiragana|katakana|hiragana-iroha|katakana-iroha)",
        re.I,
    )
    output: list[str] = []
    stack: list[tuple[str, str]] = []
    position = 0
    smiley_pattern = (
        re.compile("|".join(re.escape(code) for code in sorted(smileys, key=len, reverse=True)))
        if smileys
        else None
    )

    def render_text(value: str) -> str:
        if smiley_pattern is None:
            return escape_text(value).replace("\n", "<br>\n")

        rendered: list[str] = []
        offset = 0
        for match in smiley_pattern.finditer(value):
            rendered.append(escape_text(value[offset:match.start()]).replace("\n", "<br>\n"))
            rendered.append(hold(f'<img class="smiley" src="{escape(smileys[match.group(0)])}" alt="{escape(match.group(0))}">'))
            offset = match.end()
        rendered.append(escape_text(value[offset:]).replace("\n", "<br>\n"))
        return "".join(rendered)

    for token in BBCODE_TOKEN_RE.finditer(body):
        output.append(render_text(body[position:token.start()]))
        tag = token.group("tag").lower()
        closing = token.group("close") is not None
        if tag in {"*", "+", "@", "#", "x", "o", "0"}:
            if stack and stack[-1][0] == "li":
                output.append(stack.pop()[1])
            if any(open_tag == "list" for open_tag, _ in stack):
                output.append("<li>")
                stack.append(("li", "</li>"))
            else:
                output.append(escape_text(token.group(0)))
            position = token.end()
            continue
        if tag in wrappers:
            opening, ending = wrappers[tag]
            argument = (token.group("arg") or "").strip()
            if not closing and tag in {"size", "font", "color", "bdo", "abbr", "acronym", "anchor", "list", "me", "float"}:
                opening = None
                if tag == "size" and SIZE_VALUE_RE.fullmatch(argument):
                    opening = f'<span style="font-size:{argument.lower()}">'
                elif tag == "size" and argument in {"1", "2", "3", "4", "5", "6", "7"}:
                    em_size = {"1": "0.7em", "2": "1em", "3": "1.35em", "4": "1.45em",
                               "5": "2em", "6": "2.65em", "7": "3.95em"}[argument]
                    opening = f'<span style="font-size:{em_size}">'
                elif tag == "font" and re.fullmatch(r"[\w ,\-]{1,80}", argument):
                    opening = f'<span style="font-family:{escape(argument)}">'
                elif tag == "color" and color_value_re.fullmatch(argument):
                    opening = f'<span style="color:{escape(argument)}">'
                elif tag == "list" and not argument:
                    opening = "<ul>"
                elif tag == "list" and argument.lower().startswith("type=") and list_type_re.fullmatch(argument[5:]):
                    opening = f'<ul style="list-style-type:{argument[5:].lower()}">'
                elif tag == "bdo" and argument.lower() in {"ltr", "rtl"}:
                    opening = f'<bdo dir="{argument.lower()}">'
                elif tag in {"abbr", "acronym"} and argument:
                    opening = f'<abbr title="{escape(argument)}">'
                elif tag == "anchor" and re.fullmatch(r"#?[A-Za-z][A-Za-z0-9_-]*", argument):
                    opening = f'<span id="post_{escape(argument.lstrip("#"))}">'
                elif tag == "me":
                    opening = f'<div class="meaction">* {escape(argument)} '
                elif tag == "float":
                    float_match = re.fullmatch(
                        r"(left|right)(?:\s+max=(\d{1,4})(%|px|em|rem|ex|pt|pc|ch|vw|vh|vmin|vmax|cm|mm|in)?)?",
                        argument,
                        re.I,
                    )
                    if float_match:
                        direction, width, unit = float_match.groups()
                        max_width = f";max-width:{width}{unit or 'px'}" if width else ""
                        opening = f'<div class="bbc_float float{direction.lower()}" style="float:{direction.lower()}{max_width}">'
                    else:
                        output.append(escape_text(token.group(0)))
                        position = token.end()
                        continue
                if opening is None:
                    output.append(escape_text(token.group(0)))
                    position = token.end()
                    continue
            elif not closing and tag == "quote":
                author_match = re.search(
                    r'(?:^|\s)author=(?:"([^"]{1,192})"|\'([^\']{1,192})\'|([^\s]+))',
                    argument,
                )
                author = next((value for value in author_match.groups() if value), "") if author_match else ""
                if author:
                    opening = f"<blockquote><cite>Quote from: {escape(author)}</cite>"
            if closing:
                if tag in {"br", "hr"}:
                    output.append(escape_text(token.group(0)))
                elif tag == "list":
                    list_index = next(
                        (index for index in range(len(stack) - 1, -1, -1) if stack[index][0] == "list"),
                        -1,
                    )
                    if list_index >= 0:
                        while len(stack) > list_index:
                            output.append(stack.pop()[1])
                    else:
                        output.append(escape_text(token.group(0)))
                elif stack and stack[-1][0] == tag:
                    output.append(stack.pop()[1])
                else:
                    output.append(escape_text(token.group(0)))
            else:
                if tag == "br":
                    output.append("<br>")
                elif tag == "hr":
                    output.append("<hr>")
                else:
                    output.append(opening)
                    stack.append((tag, ending))
        else:
            output.append(escape_text(token.group(0)))
        position = token.end()

    output.append(render_text(body[position:]))
    while stack:
        output.append(stack.pop()[1])
    rendered = "".join(output)
    for key, fragment in placeholders.items():
        rendered = rendered.replace(key, fragment)
    return rendered


def page_links(current: int, pages: int, href_for_page: Callable[[int], str]) -> str:
    if pages <= 1:
        return ""
    if pages <= 5:
        links = [
            f'<span class="current" aria-current="page">{page}</span>'
            if page == current
            else f'<a href="{escape(href_for_page(page))}">{page}</a>'
            for page in range(1, pages + 1)
        ]
        return '<nav class="pagination" aria-label="Pagination">' + " ".join(links) + "</nav>"

    links = [f'<a href="{escape(href_for_page(1))}">First</a>']
    links.append(
        f'<a href="{escape(href_for_page(current - 1))}" rel="prev">Previous</a>'
        if current > 1
        else '<span class="disabled" aria-disabled="true">Previous</span>'
    )

    start = max(1, current - 2)
    end = min(pages, current + 2)
    if start > 1:
        links.append('<span class="ellipsis" aria-hidden="true">…</span>')
    for page in range(start, end + 1):
        if page == current:
            links.append(f'<span class="current" aria-current="page">{page}</span>')
        else:
            links.append(f'<a href="{escape(href_for_page(page))}">{page}</a>')
    if end < pages:
        links.append('<span class="ellipsis" aria-hidden="true">…</span>')

    links.append(
        f'<a href="{escape(href_for_page(current + 1))}" rel="next">Next</a>'
        if current < pages
        else '<span class="disabled" aria-disabled="true">Next</span>'
    )
    links.append(f'<a href="{escape(href_for_page(pages))}">Last</a>')
    links.append(
        f'<form class="page-jump" aria-label="Go to page" data-pages="{pages}">'
        f'<label>Page <input type="number" name="page" min="1" max="{pages}" step="1" '
        f'value="{current}" required></label><button type="submit">Go</button></form>'
    )
    return '<nav class="pagination" aria-label="Pagination">' + " ".join(links) + "</nav>"


def layout(
    title: str,
    content: str,
    css_href: str,
    breadcrumbs: str = "",
    home_href: str = "index.html",
    logo_href: str = "",
) -> str:
    brand = (
        f'<a class="site-brand" href="{escape(home_href)}"><img src="{escape(logo_href)}" alt="">'
        "Forum archive</a>"
        if logo_href
        else f'<a class="site-brand" href="{escape(home_href)}">Forum archive</a>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="stylesheet" href="{escape(css_href)}">
<script src="{escape(str(Path(css_href).parent / "archive.js"))}" defer></script>
</head>
<body>
<header class="site-header">{brand}</header>
<main>
{breadcrumbs}
{content}
</main>
<footer>Static archive generated from the forum database.</footer>
</body>
</html>
"""


def breadcrumb(*items: tuple[str, str | None]) -> str:
    bits = []
    for label, href in items:
        bits.append(
            f'<a href="{escape(href)}">{escape_forum_text(label)}</a>'
            if href
            else f"<span>{escape_forum_text(label)}</span>"
        )
    return '<nav class="breadcrumbs" aria-label="Breadcrumbs">' + " / ".join(bits) + "</nav>"


class ForumExporter:
    def __init__(self, args: argparse.Namespace, connection: Any):
        self.args = args
        self.connection = connection
        self.prefix = args.db_prefix
        if not IDENTIFIER_RE.fullmatch(self.prefix):
            raise ExportError("Database table prefix must contain only letters, digits, and underscores.")
        self.output = args.output.resolve()
        self.media_dir = (self.output / args.media_dir).resolve()
        if not self.media_dir.is_relative_to(self.output):
            raise ExportError("media_dir must be a relative path inside output_dir.")
        self.logo_path = args.logo_path
        self.logo_archive_name: str | None = None
        if self.logo_path is not None:
            suffix = self.logo_path.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = ".img"
            self.logo_archive_name = f"forum-logo{suffix}"
        self.attach_dirs = {int(folder): Path(path).resolve() for folder, path in args.attachment_dirs.items()}
        self.media_sources = {
            name: {
                "path": Path(source["path"]).resolve(),
                "url_prefix": source.get("url_prefix", ""),
            }
            for name, source in args.media_sources.items()
        }
        for name in self.media_sources:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ExportError(f"Invalid media source name {name!r}; use letters, digits, underscores, or hyphens.")
        self.media_url_prefixes = sorted(
            (
                (source["url_prefix"], name, source["path"])
                for name, source in self.media_sources.items()
                if source["url_prefix"]
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        self.smileys: dict[str, str] = {}
        self.post_ranks: list[tuple[int, str]] = []
        self.avatar_cache: dict[int, str] = {}
        self.default_avatar_path: str | None = None
        self.topic_counts: dict[int, int] = {}
        self.total_topics = 0
        self.exported_topics = 0
        self.skipped_attachments = 0
        self.unmapped_attachment_folders: set[int] = set()

    def table(self, name: str) -> str:
        return f"`{self.prefix}{name}`"

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = self.connection.cursor(dictionary=True)
        try:
            cursor.execute(sql, params)
            return cursor.fetchall()
        finally:
            cursor.close()

    def guest_boards(self) -> list[Board]:
        settings = self.query(
            f"SELECT value FROM {self.table('settings')} WHERE variable = 'deny_boards_access' LIMIT 1"
        )
        deny_enabled = bool(settings and str(settings[0]["value"]) not in {"", "0"})
        deny_clause = (
            f"""AND NOT EXISTS (
                SELECT 1 FROM {self.table('board_permissions_view')} bpvd
                WHERE bpvd.id_board = b.id_board AND bpvd.id_group = -1 AND bpvd.deny = 1
            )"""
            if deny_enabled
            else ""
        )
        rows = self.query(
            f"""SELECT id_board, id_cat, id_parent, name, description
            FROM {self.table('boards')} b
            WHERE EXISTS (
                SELECT 1 FROM {self.table('board_permissions_view')} bpva
                WHERE bpva.id_board = b.id_board AND bpva.id_group = -1 AND bpva.deny = 0
            )
            {deny_clause}
            AND b.redirect = ''
            ORDER BY b.id_cat, b.board_order, b.id_board"""
        )
        return [
            Board(int(row["id_board"]), int(row["id_cat"]), int(row["id_parent"]), row["name"], row["description"])
            for row in rows
        ]

    def categories(self, board_ids: list[int]) -> list[dict[str, Any]]:
        if not board_ids:
            return []
        placeholders = ",".join(["%s"] * len(board_ids))
        return self.query(
            f"""SELECT DISTINCT c.id_cat, c.name, c.description, c.cat_order
            FROM {self.table('categories')} c
            JOIN {self.table('boards')} b ON b.id_cat = c.id_cat
            WHERE b.id_board IN ({placeholders})
            ORDER BY c.cat_order, c.id_cat""",
            tuple(board_ids),
        )

    def ensure_output(self) -> None:
        (self.output / "assets").mkdir(parents=True, exist_ok=True)
        (self.output / "boards").mkdir(exist_ok=True)
        (self.output / "topics").mkdir(exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        (self.output / "assets" / "style.css").write_text(STYLE, encoding="utf-8")
        (self.output / "assets" / "archive.js").write_text(ARCHIVE_JS, encoding="utf-8")
        if self.logo_path is not None:
            if not self.logo_path.is_file():
                raise ExportError(f"Configured logo file does not exist: {self.logo_path}")
            if self.logo_path.is_symlink():
                raise ExportError(f"Configured logo file must not be a symlink: {self.logo_path}")
            logo_target = self.media_dir / self.logo_archive_name
            shutil.copyfile(self.logo_path, logo_target)
        for name, source in self.media_sources.items():
            source_dir = source["path"]
            if not source_dir.is_dir():
                raise ExportError(f"Configured media source directory does not exist: {source_dir}")
            if self.output == source_dir or self.output.is_relative_to(source_dir):
                raise ExportError(f"Output directory must not be inside media source directory {source_dir}")
            target_dir = self.media_dir / name
            resolved_target = target_dir.resolve()
            if (
                resolved_target == source_dir
                or resolved_target.is_relative_to(source_dir)
                or source_dir.is_relative_to(resolved_target)
            ):
                raise ExportError(
                    f"Configured media source {source_dir} overlaps its archive destination {resolved_target}"
                )
            target_dir.mkdir(parents=True, exist_ok=True)
            for current, directories, filenames in os.walk(source_dir, followlinks=False):
                current_path = Path(current)
                directories[:] = [
                    directory for directory in directories
                    if not (current_path / directory).is_symlink()
                ]
                relative = current_path.relative_to(source_dir)
                target = target_dir / relative
                target.mkdir(parents=True, exist_ok=True)
                for filename in filenames:
                    source_file = current_path / filename
                    if source_file.is_symlink() or not source_file.is_file():
                        continue
                    destination = target / filename
                    shutil.copyfile(source_file, destination)

    def load_smileys(self) -> None:
        settings = self.query(
            f"SELECT value FROM {self.table('settings')} WHERE variable = 'smiley_sets_default' LIMIT 1"
        )
        if not settings:
            return
        smiley_set = str(settings[0]["value"])
        rows = self.query(
            f"""SELECT s.code, f.filename
            FROM {self.table('smileys')} s
            JOIN {self.table('smiley_files')} f ON f.id_smiley = s.id_smiley
            WHERE f.smiley_set = %s
            ORDER BY CHAR_LENGTH(s.code) DESC""",
            (smiley_set,),
        )
        source = self.media_sources.get("smileys")
        if source is None:
            return
        smiley_root = source["path"]
        smiley_set_path = Path(smiley_set)
        if smiley_set_path.is_absolute() or ".." in smiley_set_path.parts:
            return
        for row in rows:
            code = str(row["code"])
            filename = Path(str(row["filename"]))
            if not code or filename.is_absolute() or ".." in filename.parts:
                continue
            relative = smiley_set_path / filename
            smiley_file = smiley_root / relative
            try:
                smiley_file.resolve().relative_to(smiley_root)
            except ValueError:
                continue
            if smiley_file.is_file() and not smiley_file.is_symlink():
                self.smileys[code] = f"../../{self.archive_media_path('smileys', relative)}"

    def load_post_ranks(self) -> None:
        rows = self.query(
            f"""SELECT group_name, min_posts
            FROM {self.table('membergroups')}
            WHERE min_posts >= 0
            ORDER BY min_posts"""
        )
        self.post_ranks = [(int(row["min_posts"]), str(row["group_name"])) for row in rows]

    def post_rank(self, post_count: int) -> str:
        matching = [name for threshold, name in self.post_ranks if threshold <= post_count]
        return matching[-1] if matching else ""

    def archive_media_path(self, name: str, relative: Path) -> str:
        return (Path(self.args.media_dir) / name / relative).as_posix()

    def resolve_local_image(self, image_url: str) -> str | None:
        try:
            parsed = urlsplit(image_url)
        except ValueError:
            return None
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return None
        image_path = unquote(parsed.path)
        for prefix, name, source_root in self.media_url_prefixes:
            prefix_parts = urlsplit(prefix)
            if (parsed.netloc and parsed.netloc != prefix_parts.netloc) or not image_path.startswith(prefix_parts.path):
                continue
            relative = Path(image_path[len(prefix_parts.path):])
            if relative.is_absolute() or ".." in relative.parts:
                return None
            source_file = source_root / relative
            try:
                source_file.resolve().relative_to(source_root)
            except ValueError:
                return None
            if source_file.is_file():
                return f"../../{self.archive_media_path(name, relative)}"
        return None

    def run(self) -> None:
        self.ensure_output()
        boards = self.guest_boards()
        self.load_smileys()
        self.load_post_ranks()
        self.topic_counts = {board.board_id: self.topic_count(board.board_id) for board in boards}
        self.total_topics = sum(self.topic_counts.values())
        self.exported_topics = 0
        board_ids = [board.board_id for board in boards]
        categories = self.categories(board_ids)
        board_by_id = {board.board_id: board for board in boards}
        category_boards: dict[int, list[Board]] = {}
        for board in boards:
            category_boards.setdefault(board.category_id, []).append(board)
        self.write_index(categories, category_boards, board_by_id)
        for board in boards:
            self.write_board(board)
        if self.skipped_attachments:
            folders = ", ".join(map(str, sorted(self.unmapped_attachment_folders))) or "mapped folders with missing or invalid files"
            print(
                f"Warning: {self.skipped_attachments} attachment(s) were not copied; "
                f"check config attachment_dirs and source files for folder(s): {folders}.",
                file=sys.stderr,
            )
        print(f"Exported {len(categories)} categories and {len(boards)} guest-visible forums to {self.output}")

    def write_index(self, categories: list[dict[str, Any]], category_boards: dict[int, list[Board]], board_by_id: dict[int, Board]) -> None:
        sections = []
        for category in categories:
            entries = []
            for board in category_boards.get(int(category["id_cat"]), []):
                parent = board_by_id.get(board.parent_id)
                indent = " child" if parent else ""
                href = f"boards/{board.board_id}/index.html"
                count = self.topic_count(board.board_id)
                entries.append(
                    f'<li class="forum{indent}"><a href="{escape(href)}">{escape_forum_text(board.name)}</a>'
                    f'<span class="muted">{count} topics</span>'
                    f'<p>{escape_forum_text(board.description)}</p></li>'
                )
            if entries:
                sections.append(
                    f'<section class="category"><h2>{escape_forum_text(category["name"])}</h2>'
                    f'<p>{escape_forum_text(category["description"])}</p><ul class="forum-list">{"".join(entries)}</ul></section>'
                )
        content = '<h1>Forum archive</h1><p class="intro">Browse the public categories, forums, topics, and posts.</p>' + "".join(sections)
        logo_href = f"{Path(self.args.media_dir) / self.logo_archive_name}" if self.logo_archive_name else ""
        document = layout("Forum archive", content, "assets/style.css", logo_href=logo_href)
        (self.output / "index.html").write_text(document, encoding="utf-8")

    def topic_count(self, board_id: int) -> int:
        if board_id in self.topic_counts:
            return self.topic_counts[board_id]
        rows = self.query(
            f"""SELECT COUNT(*) AS total FROM {self.table('topics')} t
            JOIN {self.table('messages')} m ON m.id_msg = t.id_first_msg
            WHERE t.id_board = %s AND t.approved = 1 AND m.approved = 1""",
            (board_id,),
        )
        total = int(rows[0]["total"])
        self.topic_counts[board_id] = total
        return total

    def write_board(self, board: Board) -> None:
        topic_total = self.topic_count(board.board_id)
        pages = max(1, (topic_total + self.args.topics_per_page - 1) // self.args.topics_per_page)
        directory = self.output / "boards" / str(board.board_id)
        directory.mkdir(parents=True, exist_ok=True)

        for page in range(1, pages + 1):
            offset = (page - 1) * self.args.topics_per_page
            topics = self.query(
                f"""SELECT t.id_topic,
                    (SELECT GREATEST(COUNT(*) - 1, 0) FROM {self.table('messages')} reply
                     WHERE reply.id_topic = t.id_topic AND reply.approved = 1) AS reply_count,
                    t.num_views, t.is_sticky, m.subject,
                    COALESCE(NULLIF(mem.real_name, ''), m.poster_name) AS starter_name
                FROM {self.table('topics')} t
                JOIN {self.table('messages')} m ON m.id_msg = t.id_first_msg
                LEFT JOIN {self.table('members')} mem ON mem.id_member = t.id_member_started
                WHERE t.id_board = %s AND t.approved = 1 AND m.approved = 1
                ORDER BY t.is_sticky DESC, t.id_last_msg DESC
                LIMIT %s OFFSET %s""",
                (board.board_id, self.args.topics_per_page, offset),
            )
            rows = []
            for topic in topics:
                self.exported_topics += 1
                print(f"Exporting topic {self.exported_topics} of {self.total_topics}", flush=True)
                topic_href = f"../../topics/{int(topic['id_topic'])}/index.html"
                sticky = '<span class="badge">Pinned</span> ' if topic["is_sticky"] else ""
                rows.append(
                    f'<tr><td>{sticky}<a href="{escape(topic_href)}">{escape_forum_text(topic["subject"])}</a></td>'
                    f'<td>{escape_forum_text(topic["starter_name"])}</td><td>{int(topic["reply_count"])}</td>'
                    f'<td>{int(topic["num_views"])}</td></tr>'
                )
                self.write_topic(int(topic["id_topic"]), board)

            href = "index.html" if page == 1 else f"page-{page}.html"
            paginator = page_links(page, pages, lambda p: "index.html" if p == 1 else f"page-{p}.html")
            content = (
                f"<h1>{escape_forum_text(board.name)}</h1><p>{escape_forum_text(board.description)}</p>"
                f'<p class="muted">{topic_total} topics</p>{paginator}'
                '<div class="table-wrap"><table><thead><tr><th>Topic</th><th>Started by</th><th>Replies</th><th>Views</th></tr></thead>'
                f"<tbody>{''.join(rows) or NO_TOPICS_ROW}</tbody></table></div>{paginator}"
            )
            crumbs = breadcrumb(("Forum archive", "../../index.html"), (board.name, None))
            logo_href = f"../../{Path(self.args.media_dir) / self.logo_archive_name}" if self.logo_archive_name else ""
            document = layout(
                board.name,
                content,
                "../../assets/style.css",
                crumbs,
                "../../index.html",
                logo_href,
            )
            (directory / href).write_text(document, encoding="utf-8")

    def write_topic(self, topic_id: int, board: Board) -> None:
        rows = self.query(
            f"SELECT COUNT(*) AS total FROM {self.table('messages')} WHERE id_topic = %s AND approved = 1",
            (topic_id,),
        )
        post_total = int(rows[0]["total"])
        pages = max(1, (post_total + self.args.posts_per_page - 1) // self.args.posts_per_page)
        directory = self.output / "topics" / str(topic_id)
        directory.mkdir(parents=True, exist_ok=True)
        topic_title_rows = self.query(
            f"""SELECT m.subject FROM {self.table('topics')} t
            JOIN {self.table('messages')} m ON m.id_msg = t.id_first_msg
            WHERE t.id_topic = %s""",
            (topic_id,),
        )
        topic_title = topic_title_rows[0]["subject"] if topic_title_rows else "Topic"

        for page in range(1, pages + 1):
            posts = self.query(
                f"""SELECT m.id_msg, m.poster_time, m.poster_name, m.body,
                    COALESCE(NULLIF(mem.real_name, ''), m.poster_name) AS display_name,
                    COALESCE(mem.posts, 0) AS member_posts, m.id_member
                FROM {self.table('messages')} m
                LEFT JOIN {self.table('members')} mem ON mem.id_member = m.id_member
                WHERE m.id_topic = %s AND m.approved = 1
                ORDER BY m.id_msg
                LIMIT %s OFFSET %s""",
                (topic_id, self.args.posts_per_page, (page - 1) * self.args.posts_per_page),
            )
            member_ids = [int(post["id_member"]) for post in posts if post["id_member"]]
            self.copy_avatars(member_ids)
            message_ids = [int(post["id_msg"]) for post in posts]
            attachments = self.copy_attachments(message_ids)
            rendered_posts = []
            for post in posts:
                post_attachments = attachments.get(int(post["id_msg"]), [])
                attachment_links = {
                    a["id"]: {"href": f"../../{a['href']}", "name": a["name"]}
                    for a in post_attachments
                }
                avatar = self.avatar_cache.get(int(post["id_member"]), "")
                avatar_src = avatar if safe_url(avatar) else f"../../{avatar}" if avatar else ""
                avatar_markup = f'<img class="avatar" src="{escape(avatar_src)}" alt="">' if avatar_src else '<span class="avatar-placeholder" aria-hidden="true"></span>'
                attachment_markup = ""
                if post_attachments:
                    attachment_markup = '<ul class="attachments">' + "".join(
                        f'<li><a href="../../{relative_url(item["href"])}" download="{escape(item["name"])}">{escape(item["name"])}</a></li>'
                        for item in post_attachments
                    ) + "</ul>"
                post_datetime = datetime.fromtimestamp(int(post["poster_time"]), timezone.utc)
                timestamp_text = post_datetime.strftime("%Y-%m-%d %H:%M")
                rank = self.post_rank(int(post["member_posts"]))
                rank_markup = f'<span class="rank">{escape(rank)}</span> · ' if rank else ""
                member_stats = f'<div class="muted">{rank_markup}{int(post["member_posts"])} posts</div>'
                post_content = format_post(post["body"], attachment_links, self.resolve_local_image, self.smileys)
                rendered_posts.append(
                    f'<article class="post" id="post-{int(post["id_msg"])}"><header class="post-header">{avatar_markup}'
                    f'<div><strong>{escape_forum_text(post["display_name"])}</strong>{member_stats}'
                    f'<time class="muted post-time" datetime="{post_datetime.isoformat()}" title="UTC">{timestamp_text}</time></div></header>'
                    f'<div class="post-body">{post_content}</div>{attachment_markup}</article>'
                )
            paginator = page_links(page, pages, lambda p: "index.html" if p == 1 else f"page-{p}.html")
            crumbs = breadcrumb(
                ("Forum archive", "../../index.html"),
                (board.name, f"../../boards/{board.board_id}/index.html"),
                (topic_title, None),
            )
            content = (
                f"<h1>{escape_forum_text(topic_title)}</h1><p class=\"muted\">{post_total} posts · page {page} of {pages}</p>"
                f"{paginator}{''.join(rendered_posts)}{paginator}"
            )
            logo_href = f"../../{Path(self.args.media_dir) / self.logo_archive_name}" if self.logo_archive_name else ""
            document = layout(
                str(topic_title),
                content,
                "../../assets/style.css",
                crumbs,
                "../../index.html",
                logo_href,
            )
            (directory / ("index.html" if page == 1 else f"page-{page}.html")).write_text(document, encoding="utf-8")

    def copy_attachments(self, message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not message_ids:
            return {}
        placeholders = ",".join(["%s"] * len(message_ids))
        rows = self.query(
            f"""SELECT id_attach, id_msg, id_folder, filename, file_hash
            FROM {self.table('attachments')}
            WHERE id_msg IN ({placeholders}) AND attachment_type = 0 AND approved = 1
            ORDER BY id_attach""",
            tuple(message_ids),
        )
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            source_dir = self.attach_dirs.get(int(row["id_folder"]))
            file_hash = str(row["file_hash"] or "")
            if source_dir is None or not re.fullmatch(r"[A-Fa-f0-9]{40}", file_hash):
                self.skipped_attachments += 1
                self.unmapped_attachment_folders.add(int(row["id_folder"]))
                continue
            source = source_dir / f'{int(row["id_attach"])}_{file_hash}.dat'
            if not source.is_file():
                self.skipped_attachments += 1
                self.unmapped_attachment_folders.add(int(row["id_folder"]))
                continue
            name = attachment_filename(row["filename"])
            target_name = f'{int(row["id_attach"])}-{name}'
            target = self.media_dir / "attachments" / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            result.setdefault(int(row["id_msg"]), []).append(
                {
                    "id": int(row["id_attach"]),
                    "href": f"{Path(self.args.media_dir) / 'attachments' / target_name}",
                    "name": name,
                }
            )
        return result

    def copy_avatars(self, member_ids: list[int]) -> None:
        pending = sorted(set(member_ids) - self.avatar_cache.keys())
        if not pending:
            return
        placeholders = ",".join(["%s"] * len(pending))
        members = self.query(
            f"SELECT id_member, avatar FROM {self.table('members')} WHERE id_member IN ({placeholders})",
            tuple(pending),
        )
        avatars = {int(row["id_member"]): row["avatar"] for row in members}
        custom_rows = self.query(
            f"""SELECT id_member, id_attach, id_folder, filename, file_hash
            FROM {self.table('attachments')}
            WHERE id_member IN ({placeholders}) AND attachment_type = 1
            ORDER BY id_attach DESC""",
            tuple(pending),
        )
        custom_files: dict[int, dict[str, Any]] = {}
        for row in custom_rows:
            custom_files.setdefault(int(row["id_member"]), row)
        for member_id in pending:
            avatar_file = None
            custom = custom_files.get(member_id)
            if custom:
                source_dir = self.attach_dirs.get(int(custom["id_folder"]))
                file_hash = str(custom["file_hash"] or "")
                if source_dir and re.fullmatch(r"[A-Fa-f0-9]{40}", file_hash):
                    candidate = source_dir / f'{int(custom["id_attach"])}_{file_hash}.dat'
                    if candidate.is_file() and not candidate.is_symlink():
                        extension = Path(str(custom["filename"])).suffix
                        avatar_file = (candidate, extension if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", extension) else ".img")
                custom_avatar_source = self.media_sources.get("custom_avatars")
                if avatar_file is None and custom_avatar_source:
                    filename = Path(str(custom["filename"])).name
                    candidate = custom_avatar_source["path"] / filename
                    if candidate.is_file() and not candidate.is_symlink():
                        avatar_file = (
                            candidate,
                            candidate.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", candidate.suffix) else ".img",
                        )
            avatar_name = str(avatars.get(member_id) or "")
            try:
                avatar_is_url = bool(urlsplit(avatar_name).scheme)
            except ValueError:
                avatar_is_url = False
            avatar_source = self.media_sources.get("avatars")
            if avatar_file is None and avatar_name and not avatar_is_url and avatar_source:
                candidate = avatar_source["path"] / Path(avatar_name).name
                if candidate.is_file() and not candidate.is_symlink():
                    avatar_file = (candidate, candidate.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", candidate.suffix) else ".img")
            custom_avatar_source = self.media_sources.get("custom_avatars")
            if avatar_file is None and custom_avatar_source and avatar_name:
                candidate = custom_avatar_source["path"] / Path(avatar_name).name
                if candidate.is_file() and not candidate.is_symlink():
                    avatar_file = (candidate, candidate.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", candidate.suffix) else ".img")
            if avatar_file:
                source, suffix = avatar_file
                target_name = f"{member_id}{suffix.lower()}"
                target = self.media_dir / "avatars" / target_name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                self.avatar_cache[member_id] = f"{Path(self.args.media_dir) / 'avatars' / target_name}"
            else:
                external_avatar = safe_url(avatar_name)
                try:
                    parsed_avatar = urlsplit(avatar_name)
                except ValueError:
                    parsed_avatar = None
                if external_avatar and parsed_avatar and parsed_avatar.scheme.lower() in {"http", "https"} and parsed_avatar.netloc:
                    self.avatar_cache[member_id] = external_avatar
                else:
                    self.avatar_cache[member_id] = self.copy_default_avatar()

    def copy_default_avatar(self) -> str:
        if self.default_avatar_path is not None:
            return self.default_avatar_path
        avatar_source = self.media_sources.get("avatars")
        if avatar_source is None:
            self.default_avatar_path = ""
            return self.default_avatar_path
        source = avatar_source["path"] / "default.png"
        if not source.is_file() or source.is_symlink():
            self.default_avatar_path = ""
            return self.default_avatar_path
        target = self.media_dir / "avatars" / "default.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        self.default_avatar_path = f"{Path(self.args.media_dir) / 'avatars' / 'default.png'}"
        return self.default_avatar_path


STYLE = """
:root{color-scheme:light;--ink:#202b38;--muted:#657385;--line:#dce3ea;--accent:#315f8c;--surface:#fff;--page:#f3f6f8}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.site-header{background:#20384e;padding:1rem max(1rem,calc((100% - 1050px)/2));font-size:1.1rem;font-weight:700}.site-header a{color:white}
main{max-width:1050px;margin:2rem auto;padding:0 1rem}h1{font-size:2rem;line-height:1.2;margin:.4rem 0 1rem}h2{font-size:1.35rem;margin:.2rem 0}
.breadcrumbs,.muted,footer{color:var(--muted)}.breadcrumbs{margin:0 0 1rem}.intro{color:var(--muted)}.category,.post{background:var(--surface);border:1px solid var(--line);border-radius:8px;margin:1rem 0;padding:1rem}
.forum-list{list-style:none;padding:0;margin:.5rem 0 0}.forum{border-top:1px solid var(--line);padding:.8rem 0}.forum.child{padding-left:1.5rem}.forum p{margin:.2rem 0;color:var(--muted)}.forum .muted{float:right}
.table-wrap{overflow-x:auto}table{border-collapse:collapse;background:var(--surface);width:100%;border:1px solid var(--line)}th,td{text-align:left;padding:.75rem;border-bottom:1px solid var(--line)}th{background:#eaf0f5}.badge{font-size:.75rem;background:#eaf0f5;border-radius:1rem;padding:.15rem .5rem}
.pagination{display:flex;gap:.35rem;flex-wrap:wrap;margin:1rem 0}.pagination a,.pagination span{padding:.35rem .65rem;border:1px solid var(--line);border-radius:5px;background:white}.pagination .current{background:var(--accent);color:white}
.pagination .disabled,.pagination .ellipsis{color:var(--muted)}.page-jump{display:inline-flex;align-items:center;gap:.35rem;margin-left:.4rem}.page-jump label{display:flex;align-items:center;gap:.3rem;color:var(--muted);font-size:.9rem}.page-jump input{width:4.5rem;padding:.35rem;border:1px solid var(--line);border-radius:5px;font:inherit}.page-jump button{padding:.35rem .6rem;border:1px solid var(--line);border-radius:5px;background:white;color:var(--accent);font:inherit;cursor:pointer}
.site-brand{display:inline-flex;align-items:center;gap:.8rem}.site-brand img{max-width:min(400px,60vw);max-height:72px;width:auto;height:auto;object-fit:contain}.post-header{display:flex;align-items:center;gap:.8rem;border-bottom:1px solid var(--line);padding-bottom:.8rem}.avatar,.avatar-placeholder{width:48px;height:48px;border-radius:0;object-fit:cover;background:#e6ebf0;flex:none}.post-body{padding:1rem 0;overflow-wrap:anywhere}.post-body blockquote{border-left:3px solid #bdcbd8;margin:.8rem 0;padding:.3rem 1rem;color:#526274;background:#f7f9fb}.post-body pre{white-space:pre-wrap;background:#202b38;color:#f4f6f8;padding:1rem;border-radius:5px}.post-image{max-width:100%;height:auto}.smiley{vertical-align:middle;max-width:2em;max-height:2em}
.attachments{border-top:1px solid var(--line);padding-top:.6rem;margin-bottom:0}footer{max-width:1050px;margin:2rem auto;padding:0 1rem 2rem;font-size:.9rem}
@media(max-width:640px){main{margin:1rem auto}h1{font-size:1.6rem}th,td{padding:.55rem;font-size:.9rem}.forum .muted{float:none;display:block}}
"""

ARCHIVE_JS = """
document.addEventListener("submit", (event) => {
  const form = event.target.closest(".page-jump");
  if (!form) return;
  event.preventDefault();

  const input = form.elements.page;
  const page = Number.parseInt(input.value, 10);
  const total = Number.parseInt(form.dataset.pages, 10);
  if (!Number.isInteger(page) || page < 1 || page > total) {
    input.reportValidity();
    return;
  }

  const directory = window.location.pathname.slice(0, window.location.pathname.lastIndexOf("/") + 1);
  window.location.assign(directory + (page === 1 ? "index.html" : `page-${page}.html`));
});
"""


def load_config(config_path: Path) -> argparse.Namespace:
    config_path = config_path.resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ExportError(f"Could not read config file {config_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ExportError(f"Invalid JSON in config file {config_path}: {error}") from error

    if not isinstance(config, dict):
        raise ExportError("The config file must contain a JSON object.")
    database = config.get("database")
    if not isinstance(database, dict):
        raise ExportError("Config requires a 'database' object.")
    for key in ("host", "name", "user"):
        if not isinstance(database.get(key), str) or not database[key]:
            raise ExportError(f"Config database.{key} must be a non-empty string.")

    def configured_path(value: Any, label: str) -> Path:
        if not isinstance(value, str) or not value:
            raise ExportError(f"Config {label} must be a non-empty path string.")
        path = Path(value)
        return (config_path.parent / path).resolve() if not path.is_absolute() else path.resolve()

    output = configured_path(config.get("output_dir", "site"), "output_dir")
    media_dir = config.get("media_dir", "media")
    if not isinstance(media_dir, str) or not media_dir or Path(media_dir).is_absolute():
        raise ExportError("Config media_dir must be a non-empty relative path inside output_dir.")
    raw_logo_path = config.get("logo_path")
    logo_path = configured_path(raw_logo_path, "logo_path") if raw_logo_path is not None else None

    raw_attachment_dirs = config.get("attachment_dirs", {})
    if not isinstance(raw_attachment_dirs, dict):
        raise ExportError("Config attachment_dirs must be an object mapping folder IDs to paths.")
    attachment_dirs: dict[str, Path] = {}
    for folder, value in raw_attachment_dirs.items():
        if not str(folder).isdigit():
            raise ExportError(f"Invalid attachment folder ID {folder!r}; expected a non-negative integer.")
        attachment_dirs[str(folder)] = configured_path(value, f"attachment_dirs.{folder}")

    raw_media_sources = config.get("media_sources", {})
    if not isinstance(raw_media_sources, dict):
        raise ExportError("Config media_sources must be an object mapping source names to settings.")
    media_sources: dict[str, dict[str, str]] = {}
    for name, source in raw_media_sources.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(name)):
            raise ExportError(f"Invalid media source name {name!r}; use letters, digits, underscores, or hyphens.")
        if isinstance(source, str):
            source = {"path": source}
        if not isinstance(source, dict):
            raise ExportError(f"Config media_sources.{name} must be a path string or object.")
        url_prefix = source.get("url_prefix", "")
        if not isinstance(url_prefix, str):
            raise ExportError(f"Config media_sources.{name}.url_prefix must be a string.")
        media_sources[str(name)] = {
            "path": str(configured_path(source.get("path"), f"media_sources.{name}.path")),
            "url_prefix": url_prefix,
        }

    try:
        db_port = int(database.get("port", 3306))
        topics_per_page = int(config.get("topics_per_page", 25))
        posts_per_page = int(config.get("posts_per_page", 20))
    except (TypeError, ValueError) as error:
        raise ExportError("Database port and page sizes must be integers.") from error
    if db_port < 1 or db_port > 65535:
        raise ExportError("Config database.port must be between 1 and 65535.")
    if topics_per_page < 1 or posts_per_page < 1:
        raise ExportError("Config topics_per_page and posts_per_page must be positive integers.")

    password = database.get("password", "")
    prefix = database.get("prefix", "smf_")
    if not isinstance(password, str) or not isinstance(prefix, str):
        raise ExportError("Config database.password and database.prefix must be strings.")

    return argparse.Namespace(
        config_path=config_path,
        db_host=database["host"],
        db_port=db_port,
        db_name=database["name"],
        db_user=database["user"],
        db_password=password,
        db_prefix=prefix,
        output=output,
        media_dir=media_dir,
        logo_path=logo_path,
        attachment_dirs=attachment_dirs,
        media_sources=media_sources,
        topics_per_page=topics_per_page,
        posts_per_page=posts_per_page,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=script_dir / "config.json",
        help="JSON config file (default: export/config.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    cli_args = parse_args(argv)
    try:
        args = load_config(cli_args.config)
    except ExportError as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    if mysql is None:
        print("Missing dependency: install requirements with `python3 -m pip install -r requirements.txt`.", file=sys.stderr)
        return 2
    try:
        connection = mysql.connect(
            host=args.db_host,
            port=args.db_port,
            database=args.db_name,
            user=args.db_user,
            password=args.db_password or "",
            charset="utf8mb4",
        )
        try:
            ForumExporter(args, connection).run()
        finally:
            connection.close()
    except (ExportError, OSError, mysql.Error) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
