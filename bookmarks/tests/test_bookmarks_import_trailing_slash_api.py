from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status

from bookmarks.tests.helpers import (
    BookmarkFactoryMixin,
    BookmarkHtmlTag,
    ImportTestMixin,
    LinkdingApiTestCase,
)


class BookmarksImportTrailingSlashApiTestCase(
    LinkdingApiTestCase, BookmarkFactoryMixin, ImportTestMixin
):
    def setUp(self) -> None:
        self.client.force_login(self.get_or_create_test_user())
        self.authenticate()

    def import_bookmarks(self, tags: list[BookmarkHtmlTag]):
        html = self.render_html(tags=tags)
        import_file = SimpleUploadedFile(
            "bookmarks.html", html.encode("utf-8"), content_type="text/html"
        )
        response = self.client.post(
            reverse("linkding:settings.import"),
            {"import_file": import_file},
            follow=True,
        )
        self.assertRedirects(response, reverse("linkding:settings.general"))
        return response

    def list_bookmark_urls(self):
        response = self.get(reverse("linkding:bookmark-list"))
        self.assertIn("results", response.data)
        urls = []
        for bookmark in response.data["results"]:
            self.assertIn("url", bookmark)
            urls.append(bookmark["url"])
        return urls

    def get_example_com_urls(self):
        return [
            url
            for url in self.list_bookmark_urls()
            if url.rstrip("/") == "https://example.com"
        ]

    def assertSuccessMessage(self, response, message: str):
        self.assertInHTML(
            f"""
            <div class="toast toast-success mb-4">{message}</div>
        """,
            response.content.decode("utf-8"),
        )

    def test_import_collapses_trailing_slash_variants_into_one_bookmark(self):
        self.import_bookmarks(
            [
                BookmarkHtmlTag(
                    href="https://example.com", title="Example without slash"
                ),
                BookmarkHtmlTag(
                    href="https://example.com/", title="Example with slash"
                ),
            ]
        )

        example_urls = self.get_example_com_urls()
        self.assertEqual(
            len(example_urls),
            1,
            f"Expected one bookmark for example.com, got: {example_urls}",
        )

    def test_import_does_not_duplicate_existing_bookmark_with_trailing_slash_variant(
        self,
    ):
        self.post(
            reverse("linkding:bookmark-list")
            + "?disable_scraping&disable_html_snapshot",
            {"url": "https://example.com", "title": "Existing example"},
            status.HTTP_201_CREATED,
        )
        self.assertEqual(self.get_example_com_urls(), ["https://example.com"])

        self.import_bookmarks(
            [BookmarkHtmlTag(href="https://example.com/", title="Imported example")]
        )

        example_urls = self.get_example_com_urls()
        self.assertEqual(
            len(example_urls),
            1,
            f"Expected one bookmark for example.com, got: {example_urls}",
        )

    def test_import_of_distinct_urls_keeps_both_and_reports_count(self):
        response = self.import_bookmarks(
            [
                BookmarkHtmlTag(href="https://example.com/a", title="Example A"),
                BookmarkHtmlTag(href="https://example.com/b", title="Example B"),
            ]
        )

        self.assertSuccessMessage(response, "2 bookmarks were successfully imported.")
        self.assertCountEqual(
            self.list_bookmark_urls(),
            ["https://example.com/a", "https://example.com/b"],
        )
