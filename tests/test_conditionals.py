import os
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser


ROOT = os.path.dirname(os.path.dirname(__file__))
HPP = os.path.join(ROOT, "hpp.py")


class ParsedHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        normalized_attrs = {}
        for key, value in attrs:
            normalized_attrs[key] = "" if value is None else value
        self.tags.append((tag, normalized_attrs))

    def find(self, tag=None, **attrs):
        for tag_name, tag_attrs in self.tags:
            if tag is not None and tag_name != tag:
                continue
            matches = True
            for key, value in attrs.items():
                if tag_attrs.get(key) != value:
                    matches = False
                    break
            if matches:
                return tag_attrs
        return None


class HppIntegrationTest(unittest.TestCase):
    def run_hpp(self, files, templates):
        with tempfile.TemporaryDirectory() as tmpdir:
            site = os.path.join(tmpdir, "site")
            hpp_dir = os.path.join(site, "hpp")
            out = os.path.join(tmpdir, "out")
            os.makedirs(hpp_dir)

            for relpath, content in files.items():
                path = os.path.join(site, relpath)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fp:
                    fp.write(content)

            for name, content in templates.items():
                with open(os.path.join(hpp_dir, f"{name}.html"), "w") as fp:
                    fp.write(content)

            subprocess.run(
                [sys.executable, HPP, "--in-dir", site, "--out-dir", out],
                check=True,
                capture_output=True,
                text=True,
            )

            with open(os.path.join(out, "index.html")) as fp:
                return ParsedHTML(fp.read())


class ConditionalTests(HppIntegrationTest):
    def test_element_conditionals_keep_and_remove_nodes(self):
        soup = self.run_hpp(
            {"index.html": '<hpp template="panel" show="yes" compact="true" />'},
            {
                "panel": """
                    <section>
                      <p id="shown" hpp-if="show">Shown</p>
                      <p id="hidden" hpp-if="missing">Hidden</p>
                      <p id="compact" hpp-unless="compact">Compact</p>
                      <p id="full" hpp-unless="full">Full</p>
                    </section>
                """,
            },
        )

        shown = soup.find(id="shown")
        full = soup.find(id="full")
        self.assertIsNotNone(shown)
        self.assertIsNone(soup.find(id="hidden"))
        self.assertIsNone(soup.find(id="compact"))
        self.assertIsNotNone(full)
        self.assertNotIn("hpp-if", shown)
        self.assertNotIn("hpp-unless", full)

    def test_conditional_attrs_set_then_else_and_boolean_values(self):
        soup = self.run_hpp(
            {"index.html": '<hpp template="nav-item" key="eng" current="eng" external="true" />'},
            {
                "nav-item": """
                    <li class="nav-item"
                        hpp-class-if="key == current"
                        hpp-class-then="selected">
                      <a hpp-aria-current-if="key == current"
                         hpp-aria-current-then="page"
                         hpp-target-if="external"
                         hpp-target-then="_blank"
                         hpp-rel-if="external"
                         hpp-rel-then="noopener noreferrer"
                         hpp-data-state-if="key != current"
                         hpp-data-state-then="inactive"
                         hpp-draggable-if="external">Link</a>
                    </li>
                """,
            },
        )

        item = soup.find("li")
        link = soup.find("a")
        self.assertEqual(item.get("class"), "nav-item selected")
        self.assertEqual(link.get("aria-current"), "page")
        self.assertEqual(link.get("target"), "_blank")
        self.assertEqual(link.get("rel"), "noopener noreferrer")
        self.assertNotIn("data-state", link)
        self.assertIn("draggable", link)
        self.assertNotIn("hpp-aria-current-if", link)

    def test_conditional_attrs_use_else_values(self):
        soup = self.run_hpp(
            {"index.html": '<hpp template="image" eager="false" />'},
            {
                "image": """
                    <img src="/hero.png"
                         hpp-loading-if="eager"
                         hpp-loading-then="eager"
                         hpp-loading-else="lazy" />
                """,
            },
        )

        self.assertEqual(soup.find("img").get("loading"), "lazy")

    def test_literal_comparisons_and_negation(self):
        soup = self.run_hpp(
            {"index.html": '<hpp template="flags" mode="quiet" enabled="no" />'},
            {
                "flags": """
                    <div>
                      <p id="quiet" hpp-if="mode == 'quiet'">Quiet</p>
                      <p id="loud" hpp-if='mode == "loud"'>Loud</p>
                      <p id="disabled" hpp-if="!enabled">Disabled</p>
                    </div>
                """,
            },
        )

        self.assertIsNotNone(soup.find(id="quiet"))
        self.assertIsNone(soup.find(id="loud"))
        self.assertIsNotNone(soup.find(id="disabled"))


if __name__ == "__main__":
    unittest.main()
