import unittest

from test_conditionals import HppIntegrationTest


class SlotTests(HppIntegrationTest):
    def test_slots_insert_named_content_and_remove_missing_slots(self):
        soup = self.run_hpp(
            {
                "index.html": """
                    <hpp template="panel" title="Hello">
                      <hpp-slot name="body">
                        <p id="body" hpp-text="{title}"></p>
                      </hpp-slot>
                    </hpp>
                """,
            },
            {
                "panel": """
                    <section>
                      <h1 hpp-text="{title}"></h1>
                      <div class="body"><hpp-slot name="body" /></div>
                      <footer><hpp-slot name="actions" /></footer>
                    </section>
                """,
            },
        )

        self.assertIsNotNone(soup.find("p", id="body"))
        self.assertIsNone(soup.find("hpp-slot"))

    def test_slotted_templates_inherit_parent_args(self):
        soup = self.run_hpp(
            {
                "index.html": """
                    <hpp template="panel" title="Hello" active="true">
                      <hpp-slot name="body">
                        <hpp template="label" text="{title}" active="{active}" />
                      </hpp-slot>
                    </hpp>
                """,
            },
            {
                "panel": "<section><hpp-slot name=\"body\" /></section>",
                "label": """
                    <span id="label"
                          hpp-text="{text}"
                          hpp-class-if="active"
                          hpp-class-then="active"></span>
                """,
            },
        )

        label = soup.find("span", id="label")
        self.assertIsNotNone(label)
        self.assertEqual(label.get("class"), "active")

    def test_wrapper_template_can_provide_nav_items_once(self):
        soup = self.run_hpp(
            {"index.html": '<hpp template="site-nav" current="eng" />'},
            {
                "site-nav": """
                    <hpp template="nav" current="{current}">
                      <hpp-slot name="items">
                        <hpp template="nav-item" key="home" label="Home" href="/" />
                        <hpp template="nav-item" key="eng" label="Engineering" href="/eng" />
                      </hpp-slot>
                    </hpp>
                """,
                "nav": "<nav><ul><hpp-slot name=\"items\" /></ul></nav>",
                "nav-item": """
                    <li hpp-class-if="key == current" hpp-class-then="selected">
                      <a hpp-href="href"
                         hpp-aria-current-if="key == current"
                         hpp-aria-current-then="page">
                        <span hpp-text="{label}"></span>
                      </a>
                    </li>
                """,
            },
        )

        links = [attrs for tag, attrs in soup.tags if tag == "a"]
        items = [attrs for tag, attrs in soup.tags if tag == "li"]
        self.assertEqual(links[0].get("href"), "/")
        self.assertEqual(links[1].get("href"), "/eng")
        self.assertNotIn("class", items[0])
        self.assertEqual(items[1].get("class"), "selected")
        self.assertNotIn("aria-current", links[0])
        self.assertEqual(links[1].get("aria-current"), "page")


if __name__ == "__main__":
    unittest.main()
