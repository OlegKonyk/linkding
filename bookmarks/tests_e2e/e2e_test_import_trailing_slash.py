from django.urls import reverse
from playwright.sync_api import expect

from bookmarks.tests.helpers import BookmarkHtmlTag, ImportTestMixin
from bookmarks.tests_e2e.helpers import LinkdingE2ETestCase


class ImportTrailingSlashE2ETestCase(LinkdingE2ETestCase, ImportTestMixin):
    def locate_bookmark_items(self):
        # Bookmark entries are the list items carrying an Edit link, which leaves
        # out the pagination items in the same region
        return (
            self.page.get_by_role("region", name="Bookmark list")
            .get_by_role("listitem")
            .filter(has=self.page.get_by_role("link", name="Edit", exact=True))
        )

    def test_edit_after_importing_trailing_slash_variants_succeeds(self):
        html = self.render_html(
            tags=[
                BookmarkHtmlTag(
                    href="https://example.com", title="Example without slash"
                ),
                BookmarkHtmlTag(
                    href="https://example.com/", title="Example with slash"
                ),
            ]
        )

        # Import both URL variants from the settings page
        page = self.open(reverse("linkding:settings.general"))
        import_section = page.get_by_role("region", name="Import")
        upload_button = import_section.get_by_role("button", name="Upload")
        expect(upload_button).to_be_visible()

        import_section.locator("input[type='file']").set_input_files(
            {
                "name": "bookmarks.html",
                "mimeType": "text/html",
                "buffer": html.encode("utf-8"),
            }
        )
        upload_button.click()
        expect(
            page.get_by_text("bookmarks were successfully imported.")
        ).to_be_visible()

        # Bookmark list shows a single bookmark for example.com
        page.goto(self.live_server_url + reverse("linkding:bookmarks.index"))
        bookmark_items = self.locate_bookmark_items()
        expect(bookmark_items).to_have_count(1)

        # Edit the remaining bookmark and save it
        bookmark_items.first.get_by_role("link", name="Edit").click()
        expect(page.get_by_role("heading", name="Edit bookmark")).to_be_visible()
        page.get_by_label("Title").fill("Renamed example")
        page.get_by_role("button", name="Save", exact=True).click()

        # Wait for the save to finish: either the duplicate URL error or the list
        duplicate_error = page.get_by_text("A bookmark with this URL already exists.")
        renamed_item = bookmark_items.filter(has_text="Renamed example")
        expect(renamed_item.or_(duplicate_error)).to_be_visible()

        expect(duplicate_error).to_be_hidden()
        expect(page.get_by_role("heading", name="Edit bookmark")).to_be_hidden()
        expect(bookmark_items).to_have_count(1)
        expect(renamed_item).to_have_count(1)
