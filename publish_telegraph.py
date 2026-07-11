"""Publish documentation.md to Telegra.ph as an Instant View page.

The bot's /help sends the resulting URL, which Telegram renders as Instant View.
This script converts the Markdown into Telegra.ph nodes and publishes it. The
account token and page path are cached in .telegraph.json so re-running updates
the same page (stable URL) instead of creating a new one.

    python publish_telegraph.py
"""

import json
import os
import re
import urllib.parse
import urllib.request

API = "https://api.telegra.ph"
SOURCE = "documentation.md"
STATE = ".telegraph.json"

# Inline `code` and **bold** spans within a paragraph.
INLINE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*")


def inline_nodes(text):
    # Split a paragraph into plain strings and {code|b} elements.
    nodes = []
    pos = 0
    for match in INLINE.finditer(text):
        if match.start() > pos:
            nodes.append(text[pos : match.start()])
        code, bold = match.group(1), match.group(2)
        if code is not None:
            nodes.append({"tag": "code", "children": [code]})
        else:
            nodes.append({"tag": "b", "children": [bold]})
        pos = match.end()
    if pos < len(text):
        nodes.append(text[pos:])
    return nodes


def markdown_to_nodes(text):
    # A small line-based parser for the subset of Markdown used in the docs:
    # `# ` title (returned separately), `## ` headings, ``` fenced code blocks
    # and blank-line-separated paragraphs with inline `code`/**bold**.
    title = "Aeris"
    content = []
    paragraph = []
    code = []
    in_code = False

    def flush_paragraph():
        if paragraph:
            content.append({"tag": "p", "children": inline_nodes(" ".join(paragraph))})
            paragraph.clear()

    for line in text.splitlines():
        if line.startswith("```"):
            if in_code:
                content.append({"tag": "pre", "children": ["\n".join(code)]})
                code.clear()
            else:
                flush_paragraph()
            in_code = not in_code
        elif in_code:
            code.append(line)
        elif line.startswith("# "):
            flush_paragraph()
            title = line[2:].strip()
        elif line.startswith("## "):
            flush_paragraph()
            content.append({"tag": "h3", "children": [line[3:].strip()]})
        elif line.strip() == "":
            flush_paragraph()
        else:
            paragraph.append(line.strip())

    flush_paragraph()
    return title, content


def api_call(method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"{API}/{method}", data=data) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown error"))
    return result["result"]


def main():
    with open(SOURCE, encoding="utf-8") as f:
        title, content = markdown_to_nodes(f.read())

    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)

    if "token" not in state:
        state["token"] = api_call(
            "createAccount", short_name="Aeris", author_name="Aeris"
        )["access_token"]

    method = "editPage" if "path" in state else "createPage"
    params = dict(
        access_token=state["token"],
        title=title,
        author_name="Aeris",
        content=json.dumps(content),
    )
    if "path" in state:
        params["path"] = state["path"]
    page = api_call(method, **params)
    state["path"] = page["path"]

    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)

    print(page["url"])


if __name__ == "__main__":
    main()
