"""Markdown to HTML, the subset agents write.

Headings, paragraphs, bullet and numbered lists, fenced and inline code,
links, emphasis, block quotes, horizontal rules and pipe tables. That is
what a status report or a Claude answer uses; the rest of CommonMark
(reference links, nested lists four deep, raw HTML) is not worth a
dependency this box cannot install anyway.

Everything is escaped first and markup is added after, so a message
cannot smuggle HTML through. Links get ``rel="noopener"``; the sealed
view they render in opens them in the real browser anyway.
"""

import re
import html


_INLINE = [
    (re.compile(r"`([^`\n]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"__([^_\n]+)__"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\w)"), r"<em>\1</em>"),
    (re.compile(r"(?<![_\w])_([^_\n]+)_(?!\w)"), r"<em>\1</em>"),
    (re.compile(r"~~([^~\n]+)~~"), r"<s>\1</s>"),
]
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+|mailto:[^)\s]+)\)")
_AUTOLINK = re.compile(r"(?<![\"'>=\w])(https?://[^\s<]+[^\s<.,;:!?)\]])")


def inline(text):
    """Escape, then mark up spans."""
    out = html.escape(text, quote=False)
    # Protect code spans from the emphasis passes by rendering them first
    # and hiding their contents behind placeholders.
    codes = []

    def keep(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"
    out = re.sub(r"`([^`\n]+)`", keep, out)
    out = _LINK.sub(r'<a href="\2" rel="noopener">\1</a>', out)
    out = _AUTOLINK.sub(r'<a href="\1" rel="noopener">\1</a>', out)
    for pattern, repl in _INLINE[1:]:
        out = pattern.sub(repl, out)
    out = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>",
                 out)
    return out


def render(text):
    """A whole document."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)
    para = []

    def flush_para():
        if para:
            out.append("<p>" + inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_para()
            i += 1
            continue

        fence = re.match(r"^```\s*(\w+)?\s*$", stripped)
        if fence:
            flush_para()
            lang = fence.group(1) or ""
            block = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(block))}"
                       f"</code></pre>")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", stripped)
        if heading:
            flush_para()
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].lstrip())
                i += 1
            out.append("<blockquote>" + render("\n".join(quote)) + "</blockquote>")
            continue

        if "|" in stripped and i + 1 < n and re.match(
                r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$",
                lines[i + 1].strip()):
            flush_para()
            head = _cells(stripped)
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i].strip()))
                i += 1
            out.append("<table><thead><tr>" + "".join(
                f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r)
                          + "</tr>" for r in rows) + "</tbody></table>")
            continue

        item = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if item:
            flush_para()
            ordered = item.group(2)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items = []
            base_indent = len(item.group(1))
            while i < n:
                m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if m and len(m.group(1)) <= base_indent + 1 and \
                        (m.group(2)[0].isdigit()) == ordered:
                    items.append([m.group(3)])
                    i += 1
                    # Continuation and nested lines belong to this item.
                    while i < n and lines[i].strip() and not re.match(
                            r"^(\s*)([-*+]|\d+[.)])\s+", lines[i]) is not None \
                            and len(lines[i]) - len(lines[i].lstrip()) > base_indent:
                        items[-1].append(lines[i].strip())
                        i += 1
                    while i < n and re.match(
                            r"^(\s*)([-*+]|\d+[.)])\s+", lines[i]) and \
                            len(re.match(r"^(\s*)", lines[i]).group(1)) > base_indent + 1:
                        # A nested list: gather its lines and render inside.
                        sub = []
                        while i < n and lines[i].strip() and (
                                len(lines[i]) - len(lines[i].lstrip())
                                > base_indent + 1):
                            sub.append(lines[i][base_indent + 2:]
                                       if len(lines[i]) > base_indent + 2
                                       else lines[i].strip())
                            i += 1
                        items[-1].append("\x01" + render("\n".join(sub)))
                elif lines[i].strip() == "":
                    # A blank line ends the list unless another item follows.
                    j = i + 1
                    if j < n and re.match(r"^(\s*)([-*+]|\d+[.)])\s+", lines[j]) \
                            and len(re.match(r"^(\s*)", lines[j]).group(1)) \
                            <= base_indent + 1:
                        i = j
                        continue
                    break
                else:
                    break
            rendered = []
            for parts in items:
                text_parts = [p for p in parts if not p.startswith("\x01")]
                nested = "".join(p[1:] for p in parts if p.startswith("\x01"))
                rendered.append(f"<li>{inline(' '.join(text_parts))}{nested}</li>")
            out.append(f"<{tag}>" + "".join(rendered) + f"</{tag}>")
            continue

        para.append(line)
        i += 1

    flush_para()
    return "\n".join(out)


def _cells(row):
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


# -- the same subset, as Pango markup for a GTK label ----------------------
#
# The chat's message stream is GTK, not WebKit: a hundred rows of labels
# scroll and select like text, a hundred web views do not. So a message
# body is rendered to a short list of blocks -- a paragraph, a heading, a
# fenced code block, a quote, a list, a table -- each of them one label's
# worth of Pango markup. Inline spans go through inline() above, so the
# escaping is the one that is already tested, and the HTML tags it emits
# are mapped to Pango's.

_HTML_TO_PANGO = [
    (re.compile(r"<strong>(.*?)</strong>", re.S), r"<b>\1</b>"),
    (re.compile(r"<em>(.*?)</em>", re.S), r"<i>\1</i>"),
    (re.compile(r"<code>(.*?)</code>", re.S), r"<tt>\1</tt>"),
    (re.compile(r"<s>(.*?)</s>", re.S), r"<s>\1</s>"),
    (re.compile(r'<a href="([^"]*)" rel="noopener">(.*?)</a>', re.S),
     r'<a href="\1">\2</a>'),
]


def inline_pango(text):
    out = inline(text)
    for pattern, repl in _HTML_TO_PANGO:
        out = pattern.sub(repl, out)
    return out


def blocks(text):
    """``[(kind, markup), ...]`` for a body: kind is one of ``p``, ``h``,
    ``code``, ``quote``, ``list``, ``hr``, ``table``. Lists arrive as one
    block with a bullet or number on each line; tables as monospace."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)
    para = []

    def flush():
        if para:
            out.append(("p", inline_pango(" ".join(s.strip() for s in para))))
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        if re.match(r"^```", stripped):
            flush()
            block = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append(("code", html.escape("\n".join(block), quote=False)))
            continue
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", stripped)
        if heading:
            flush()
            out.append(("h", inline_pango(heading.group(2))))
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush()
            out.append(("hr", ""))
            i += 1
            continue
        if stripped.startswith(">"):
            flush()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].lstrip())
                i += 1
            out.append(("quote", inline_pango(" ".join(q for q in quote if q))))
            continue
        if "|" in stripped and i + 1 < n and re.match(
                r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$",
                lines[i + 1].strip()):
            flush()
            rows = [_cells(stripped)]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i].strip()))
                i += 1
            widths = [max(len(r[c]) if c < len(r) else 0 for r in rows)
                      for c in range(max(len(r) for r in rows))]
            text_rows = []
            for k, r in enumerate(rows):
                cells = [(r[c] if c < len(r) else "").ljust(widths[c])
                         for c in range(len(widths))]
                text_rows.append("  ".join(cells).rstrip())
                if k == 0:
                    text_rows.append("  ".join("─" * w for w in widths))
            out.append(("table", html.escape("\n".join(text_rows), quote=False)))
            continue
        item = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if item:
            flush()
            rows = []
            while i < n:
                m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if m:
                    depth = len(m.group(1)) // 2
                    mark = m.group(2) if m.group(2)[0].isdigit() else "•"
                    rows.append("    " * depth + f"{mark} " + inline_pango(m.group(3)))
                    i += 1
                elif lines[i].strip() and lines[i][0] in " \t" and rows:
                    rows[-1] += " " + inline_pango(lines[i].strip())
                    i += 1
                else:
                    break
            out.append(("list", "\n".join(rows)))
            continue
        para.append(line)
        i += 1
    flush()
    return out
