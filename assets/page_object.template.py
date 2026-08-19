"""Template: a page object.

The job of a page object is to make a broken selector a one-line fix in one place. If a locator
appears in three tests, a DOM change breaks three tests; if it appears here, it breaks one line.

Two rules earn their keep:

* **Expose intent, not selectors.** `open_fca_tab()` reads like the test plan; a chain of
  `.locator()` calls reads like the DOM, and the DOM is the thing that changes.
* **Encapsulate every framework workaround exactly once.** Component-library quirks — portalled
  options, pinned grid columns, on-demand toast containers — belong behind a method. Scattering them
  through tests means every future change touches every test.

Python/pytest shown; the TypeScript shape is identical with `Page`/`Locator` from `@playwright/test`.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect


class ThingWorkspace:
    def __init__(self, page: Page, entity_id: str) -> None:
        self.page = page
        # Routes are usually parameterised, which makes state deep-linkable. That is what lets a test
        # seed data over the API and jump straight to the screen under test instead of clicking
        # through thirty steps of setup it isn't trying to verify.
        self._root = f"/things/{entity_id}"

    # ---------------------------------------------------------------- navigation

    def open(self) -> "ThingWorkspace":
        self.page.goto(f"{self._root}/detail")
        # Readiness is a real assertion, not a formality. "The container is visible" and "the data has
        # arrived" are different events, and waiting on the wrong one is a race that surfaces later
        # as an intermittent CI failure.
        expect(self.main_table).to_be_visible()
        return self

    # ---------------------------------------------------------------- scoped regions

    @property
    def main_table(self) -> Locator:
        return self.page.locator("[data-testid=thing-table]")

    @property
    def toolbar(self) -> Locator:
        """Scope actions to their card.

        Repeated labels across cards ("Add", "Export", "Save") are the most common strict-mode
        failure. Anchor on something semantic rather than an index — `ancestor::` is a reverse axis,
        so `[1]` is the nearest matching ancestor.
        """
        return self.page.get_by_role("heading", name="Things").locator(
            "xpath=ancestor::*[.//button[normalize-space()='Add Thing']][1]"
        )

    # ---------------------------------------------------------------- intent

    def add_thing(self, name: str, category: str) -> None:
        self.toolbar.get_by_role("button", name="Add Thing").click()

        dialog = self.page.get_by_role("dialog").filter(
            has=self.page.get_by_role("heading", name="Add Thing")
        )
        expect(dialog).to_be_visible()
        dialog.get_by_label("Name").fill(name)

        # Options are portalled to <body>, so they are NOT inside the dialog. Querying them at page
        # level is the fix, and hiding that detail here keeps it out of every test.
        dialog.get_by_label("Category").click()
        self.page.get_by_role("option", name=category, exact=True).click()

        dialog.get_by_role("button", name="Create").click()
        # Dialog nodes commonly persist in the DOM after closing, so assert hidden, not absent.
        expect(dialog).to_be_hidden()

    def delete_thing(self, name: str) -> None:
        self.row(name).get_by_role("button", name="Actions").click()
        self.page.get_by_role("menuitem", name="Delete").click()

        # The confirm button often repeats the menu item's label — scope to the dialog, identified by
        # its body copy rather than by assuming only one dialog exists.
        confirm = self.page.get_by_role("dialog").filter(has_text="cannot be undone")
        confirm.get_by_role("button", name="Delete").click()
        expect(confirm).to_be_hidden()

    def row(self, name: str) -> Locator:
        """A row, addressed by content rather than position.

        Index-based row access is unsafe in virtualized tables: rendered order need not match display
        order, and any insert shifts it.
        """
        return self.main_table.get_by_role("row").filter(has_text=name)
