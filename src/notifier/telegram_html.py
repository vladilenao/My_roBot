"""HTML-фрагменты с ограничением длины текста, а не длины markup."""
from html import escape
from html.parser import HTMLParser


class _Characters(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.chars = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"b", "i", "u", "s", "code", "pre", "a"}:
            raise ValueError("Неподдерживаемый HTML-тег")
        attributes = "".join(f' {k}="{escape(v, quote=True)}"' for k, v in attrs if k == "href" and v is not None)
        self.stack.append((tag, f"<{tag}{attributes}>"))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("Несбалансированный HTML")
        self.stack.pop()

    def handle_data(self, data):
        self.chars.extend((char, tuple(self.stack)) for char in data)


def _render(chars):
    result, opened = [], ()
    for char, tags in chars:
        common = 0
        while common < min(len(opened), len(tags)) and opened[common] == tags[common]:
            common += 1
        result.extend(f"</{tag}>" for tag, _ in reversed(opened[common:]))
        result.extend(markup for _, markup in tags[common:])
        result.append(escape(char, quote=False))
        opened = tags
    result.extend(f"</{tag}>" for tag, _ in reversed(opened))
    return "".join(result)


def split_html(text, limit=4096):
    parser = _Characters()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ValueError("Несбалансированный HTML")
    chars = parser.chars
    chunks = []
    start = 0
    while start < len(chars):
        end, count, newline = start, 0, None
        while end < len(chars):
            size = len(chars[end][0].encode("utf-16-le")) // 2
            if count + size > limit:
                break
            count += size
            if chars[end][0] == "\n":
                newline = end + 1
            end += 1
        if end == start:
            raise ValueError("Лимит меньше одного символа")
        if end < len(chars) and newline is not None:
            end = newline
        chunks.append(_render(chars[start:end]))
        start = end
    return chunks or [""]


def visible_text(text):
    parser = _Characters()
    parser.feed(text)
    return "".join(c for c, _ in parser.chars)
