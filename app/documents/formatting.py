"""Shared Markdown parsing for readable PDF, DOCX, and plain-text output."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TypeAlias

from markdown_it import MarkdownIt


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)


Content: TypeAlias = Node | str


class _TreeParser(HTMLParser):
    _void = {"br", "hr", "img", "meta", "link", "input"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in self._void:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Node(tag, {key: value or "" for key, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")


def parse_markdown(value: object) -> list[Node]:
    """Parse Markdown as structure while treating any embedded HTML as text."""
    parser = _TreeParser()
    parser.feed(_MARKDOWN.render(str(value or "")))
    parser.close()
    return [child for child in parser.root.children if isinstance(child, Node)]


def text_content(content: list[Content]) -> str:
    return "".join(
        item if isinstance(item, str) else text_content(item.children) for item in content
    )


def clean_text(value: object) -> str:
    """Render structural Markdown as readable plain text without formatting tokens."""
    lines: list[str] = []

    def render(nodes: list[Node], depth: int = 0) -> None:
        for node in nodes:
            if node.tag == "p" and text_content(node.children).strip() == "\\pagebreak":
                lines.append("")
            elif node.tag == "p":
                lines.append(text_content(node.children).strip())
                lines.append("")
            elif node.tag.startswith("h") and node.tag[1:].isdigit():
                lines.append(text_content(node.children).strip())
                lines.append("")
            elif node.tag in {"ul", "ol"}:
                ordered = node.tag == "ol"
                start = int(node.attrs.get("start", "1"))
                item_index = 0
                for child in node.children:
                    if not isinstance(child, Node) or child.tag != "li":
                        continue
                    marker = f"{start + item_index}. " if ordered else "• "
                    item_index += 1
                    value = " ".join(
                        text_content(item.children).strip()
                        if isinstance(item, Node) and item.tag == "p"
                        else item.strip()
                        if isinstance(item, str)
                        else ""
                        for item in child.children
                        if not isinstance(item, Node) or item.tag not in {"ul", "ol"}
                    ).strip()
                    lines.append("  " * depth + marker + value)
                    for nested in child.children:
                        if isinstance(nested, Node) and nested.tag in {"ul", "ol"}:
                            render([nested], depth + 1)
                lines.append("")
            elif node.tag == "pre":
                lines.append(text_content(node.children).rstrip())
                lines.append("")
            elif node.tag == "table":
                for row in _descendants(node, "tr"):
                    cells = [
                        text_content(cell.children).strip()
                        for cell in row.children
                        if isinstance(cell, Node) and cell.tag in {"th", "td"}
                    ]
                    if cells:
                        lines.append(" | ".join(cells))
                lines.append("")
            elif node.tag == "blockquote":
                nested = clean_text_from_nodes(node.children)
                lines.extend("> " + line if line else "" for line in nested.splitlines())
                lines.append("")
            elif node.tag == "hr":
                lines.append("")
            else:
                render([child for child in node.children if isinstance(child, Node)], depth)

    render(parse_markdown(value))
    return "\n".join(lines).strip() + "\n"


def clean_text_from_nodes(nodes: list[Content]) -> str:
    parser = _TreeParser()
    parser.root.children.extend(nodes)
    # A local mini-document lets the normal renderer handle quoted block structure.
    flattened = _plain_nodes(parser.root.children)
    return flattened


def _plain_nodes(nodes: list[Content]) -> str:
    result: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            result.append(node)
        elif node.tag in {"br", "p", "div", "pre", "li", "tr"}:
            result.append(_plain_nodes(node.children) + "\n")
        elif node.tag in {"ul", "ol"}:
            for index, child in enumerate(
                child for child in node.children if isinstance(child, Node) and child.tag == "li"
            ):
                marker = f"{index + 1}. " if node.tag == "ol" else "• "
                result.append(marker + _plain_nodes(child.children).strip() + "\n")
        elif node.tag in {"th", "td"}:
            result.append(_plain_nodes(node.children) + " | ")
        else:
            result.append(_plain_nodes(node.children))
    return "".join(result).strip()


def _descendants(node: Node, tag: str) -> list[Node]:
    found: list[Node] = []
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == tag:
                found.append(child)
            found.extend(_descendants(child, tag))
    return found
