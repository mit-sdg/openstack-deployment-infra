from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DOCUMENTS = (ROOT / "README.md", *sorted(DOCS.rglob("*.md")), ROOT / "frontend" / "DESIGN.md")
LINK_RE = re.compile(r"\[[^]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^#{1,6} +(.+?) *#*$", re.MULTILINE)
FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def anchors(document: Path) -> set[str]:
    """GitHub-style heading anchors, with -1, -2 suffixes for repeats."""
    text = FENCE_RE.sub("", document.read_text())
    seen: dict[str, int] = {}
    result = set()
    for heading in HEADING_RE.findall(text):
        plain = re.sub(r"`|\*\*|\*|\[([^]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
        slug = re.sub(r"[^\w\- ]", "", plain).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


class DocumentationTests(unittest.TestCase):
    def test_links_and_anchors_resolve(self) -> None:
        for document in DOCUMENTS:
            text = FENCE_RE.sub("", document.read_text())
            for target in LINK_RE.findall(text):
                if "://" in target or target.startswith("mailto:"):
                    continue
                path, _, anchor = target.partition("#")
                linked = (document.parent / path).resolve() if path else document
                with self.subTest(document=document.relative_to(ROOT), target=target):
                    self.assertTrue(linked.exists(), "linked file is missing")
                    if anchor and linked.suffix == ".md":
                        self.assertIn(anchor, anchors(linked), "linked section is missing")


if __name__ == "__main__":
    unittest.main()
