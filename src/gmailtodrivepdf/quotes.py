"""Conservative removal of explicitly marked reply history."""

import re
from dataclasses import replace
from html.parser import HTMLParser

from .gmail import EmailMessage


class _QuoteParser(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.fragments: list[str] = []
        self.stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        quoted = (
            "gmail_quote" in (attributes.get("class") or "").split()
            or (tag == "blockquote" and (attributes.get("type") or "").lower() == "cite")
        )
        hidden = quoted or bool(self.stack and self.stack[-1][1])
        if not hidden:
            self.fragments.append(self.get_starttag_text())
        if tag not in self._VOID:
            self.stack.append((tag, hidden))

    def handle_endtag(self, tag: str) -> None:
        hidden = bool(self.stack and self.stack[-1][1])
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if not hidden:
            self.fragments.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._VOID:
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self.stack or not self.stack[-1][1]:
            self.fragments.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.handle_data(f"<!--{data}-->")


def without_quotes(message: EmailMessage) -> EmailMessage:
    body = message.html_body
    if body:
        parser = _QuoteParser()
        parser.feed(body)
        parser.close()
        body = "".join(parser.fragments)
    text = message.text_body
    if text:
        lines = text.splitlines()
        end = len(lines)
        while end and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith(">")):
            end -= 1
        # Only remove a trailing quoted run when it follows a recognisable reply header.
        header = end - 1
        while header >= 0 and not lines[header].strip():
            header -= 1
        if header >= 0 and re.fullmatch(r"On .+ wrote:", lines[header].strip()):
            if any(line.lstrip().startswith(">") for line in lines[end:]):
                text = "\n".join(lines[:header]).rstrip()
    return replace(message, html_body=body, text_body=text)
