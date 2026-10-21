import logging
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone

from bookmarks.models import Bookmark, Tag
from bookmarks.services import tasks
from bookmarks.services.parser import NetscapeBookmark, parse
from bookmarks.utils import normalize_url, parse_timestamp

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    total: int = 0
    success: int = 0
    failed: int = 0


@dataclass
class ImportOptions:
    map_private_flag: bool = False


class TagCache:
    def __init__(self, user: User):
        self.user = user
        self.cache = dict()
        # Init cache with all existing tags for that user
        tags = Tag.objects.filter(owner=user)
        for tag in tags:
            self.put(tag)

    def get(self, tag_name: str):
        tag_name_lowercase = tag_name.lower()
        if tag_name_lowercase in self.cache:
            return self.cache[tag_name_lowercase]
        else:
            return None

    def get_all(self, tag_names: list[str]):
        result = []
        for tag_name in tag_names:
            tag = self.get(tag_name)
            # Tag may not have been created if tag name exceeded maximum length
            # Prevent returning duplicates
            if tag and tag not in result:
                result.append(tag)

        return result

    def put(self, tag: Tag):
        self.cache[tag.name.lower()] = tag


class BookmarkLookup:
    # Finds bookmarks by exact URL, or else by normalized URL, so that URLs that
    # are considered equivalent (see Bookmark.query_existing) resolve to the same
    # bookmark
    def __init__(self, bookmarks):
        self.by_url = dict()
        self.by_normalized_url = dict()
        for bookmark in bookmarks:
            self.put(bookmark)

    def get(self, url: str):
        if not url:
            return None
        bookmark = self.by_url.get(url)
        if bookmark is None:
            bookmark = self.by_normalized_url.get(normalize_url(url))
        return bookmark

    def put(self, bookmark: Bookmark):
        if not bookmark.url:
            return
        # Keep the first bookmark for a URL, in case there are already duplicates
        self.by_url.setdefault(bookmark.url, bookmark)
        normalized_url = bookmark.url_normalized or normalize_url(bookmark.url)
        if normalized_url:
            self.by_normalized_url.setdefault(normalized_url, bookmark)


def import_netscape_html(
    html: str, user: User, options: ImportOptions | None = None
) -> ImportResult:
    if options is None:
        options = ImportOptions()
    result = ImportResult()
    import_start = timezone.now()

    try:
        netscape_bookmarks = parse(html)
    except Exception:
        logging.exception("Could not read bookmarks file.")
        raise

    parse_end = timezone.now()
    logger.debug(f"Parse duration: {parse_end - import_start}")

    # Create and cache all tags beforehand
    _create_missing_tags(netscape_bookmarks, user)
    tag_cache = TagCache(user)

    # Split bookmarks to import into batches, to keep memory usage for bulk operations manageable
    batches = _get_batches(netscape_bookmarks, 200)
    for batch in batches:
        _import_batch(batch, user, options, tag_cache, result)

    # Load favicons for newly imported bookmarks
    tasks.schedule_bookmarks_without_favicons(user)
    # Load previews for newly imported bookmarks
    tasks.schedule_bookmarks_without_previews(user)

    end = timezone.now()
    logger.debug(f"Import duration: {end - import_start}")

    return result


def _create_missing_tags(netscape_bookmarks: list[NetscapeBookmark], user: User):
    tag_cache = TagCache(user)
    tags_to_create = []

    for netscape_bookmark in netscape_bookmarks:
        for tag_name in netscape_bookmark.tag_names:
            # Skip tag names that exceed the maximum allowed length
            if len(tag_name) > 64:
                logger.warning(
                    f"Ignoring tag '{tag_name}' (length {len(tag_name)}) as it exceeds maximum length of 64 characters"
                )
                continue

            tag = tag_cache.get(tag_name)
            if not tag:
                tag = Tag(name=tag_name, owner=user)
                tag.date_added = timezone.now()
                tags_to_create.append(tag)
                tag_cache.put(tag)

    Tag.objects.bulk_create(tags_to_create)


def _get_batches(items: list, batch_size: int):
    batches = []
    offset = 0
    num_items = len(items)

    while offset < num_items:
        batch = items[offset : min(offset + batch_size, num_items)]
        if len(batch) > 0:
            batches.append(batch)
        offset = offset + batch_size

    return batches


def _import_batch(
    netscape_bookmarks: list[NetscapeBookmark],
    user: User,
    options: ImportOptions,
    tag_cache: TagCache,
    result: ImportResult,
):
    # Query existing bookmarks, by exact or normalized URL
    batch_urls = [bookmark.href for bookmark in netscape_bookmarks]
    bookmark_lookup = BookmarkLookup(_query_existing_bookmarks(user, batch_urls))

    # Create or update bookmarks from parsed Netscape bookmarks
    bookmarks_to_create = []
    bookmarks_to_update = []

    for netscape_bookmark in netscape_bookmarks:
        result.total = result.total + 1
        try:
            # Lookup existing bookmark by URL, or create new bookmark if there is no bookmark for that URL yet.
            # This also finds bookmarks created for an equivalent URL earlier in this batch.
            bookmark = bookmark_lookup.get(netscape_bookmark.href)
            if not bookmark:
                bookmark = Bookmark(owner=user)
            # Copy data from parsed bookmark
            _copy_bookmark_data(netscape_bookmark, bookmark, options)
            # Validate bookmark fields, exclude owner to prevent n+1 database query,
            # also there is no specific validation on owner
            bookmark.clean_fields(exclude=["owner"])
            # Schedule for update or insert, if not already scheduled for an equivalent URL
            if bookmark.pk is not None:
                if bookmark not in bookmarks_to_update:
                    bookmarks_to_update.append(bookmark)
            elif bookmark not in bookmarks_to_create:
                bookmarks_to_create.append(bookmark)
            bookmark_lookup.put(bookmark)

            result.success = result.success + 1
        except Exception:
            shortened_bookmark_tag_str = str(netscape_bookmark)[:100] + "..."
            logging.exception("Error importing bookmark: " + shortened_bookmark_tag_str)
            result.failed = result.failed + 1

    # Bulk update bookmarks in DB
    Bookmark.objects.bulk_update(
        bookmarks_to_update,
        [
            "url",
            "url_normalized",
            "date_added",
            "date_modified",
            "unread",
            "shared",
            "title",
            "description",
            "notes",
            "owner",
        ],
    )
    # Bulk insert new bookmarks into DB
    Bookmark.objects.bulk_create(bookmarks_to_create)

    # Bulk assign tags
    # In Django 3, bulk_create does not return the auto-generated IDs when bulk inserting,
    # so we have to reload the inserted bookmarks, and match them to the parsed bookmarks by URL
    bookmark_lookup = BookmarkLookup(_query_existing_bookmarks(user, batch_urls))

    BookmarkToTagRelationShip = Bookmark.tags.through
    relationships = []

    for netscape_bookmark in netscape_bookmarks:
        # Lookup bookmark by URL again
        bookmark = bookmark_lookup.get(netscape_bookmark.href)

        if not bookmark:
            # Something is wrong, we should have just created this bookmark
            shortened_bookmark_tag_str = str(netscape_bookmark)[:100] + "..."
            logging.warning(
                f"Failed to assign tags to the bookmark: {shortened_bookmark_tag_str}. Could not find bookmark by URL."
            )
            continue

        # Get tag models by string, schedule inserts for bookmark -> tag associations
        tags = tag_cache.get_all(netscape_bookmark.tag_names)
        for tag in tags:
            relationships.append(BookmarkToTagRelationShip(bookmark=bookmark, tag=tag))

    # Insert all bookmark -> tag associations at once, should ignore errors if association already exists
    BookmarkToTagRelationShip.objects.bulk_create(relationships, ignore_conflicts=True)


def _query_existing_bookmarks(user: User, urls: list[str]):
    # Same matching as Bookmark.query_existing, but for multiple URLs, also
    # include exact matches to prefer those over equivalent URLs
    urls = [url for url in urls if url]
    normalized_urls = [normalize_url(url) for url in urls]
    normalized_urls = [url for url in normalized_urls if url]
    return Bookmark.objects.filter(
        Q(owner=user) & (Q(url__in=urls) | Q(url_normalized__in=normalized_urls))
    ).order_by("id")


def _copy_bookmark_data(
    netscape_bookmark: NetscapeBookmark, bookmark: Bookmark, options: ImportOptions
):
    bookmark.url = netscape_bookmark.href
    bookmark.url_normalized = normalize_url(bookmark.url)
    if netscape_bookmark.date_added:
        bookmark.date_added = parse_timestamp(netscape_bookmark.date_added)
    else:
        bookmark.date_added = timezone.now()
    if netscape_bookmark.date_modified:
        bookmark.date_modified = parse_timestamp(netscape_bookmark.date_modified)
    else:
        bookmark.date_modified = bookmark.date_added
    bookmark.unread = netscape_bookmark.to_read
    if netscape_bookmark.title:
        bookmark.title = netscape_bookmark.title
    if netscape_bookmark.description:
        bookmark.description = netscape_bookmark.description
    if netscape_bookmark.notes:
        bookmark.notes = netscape_bookmark.notes
    if options.map_private_flag and not netscape_bookmark.private:
        bookmark.shared = True
    if netscape_bookmark.archived:
        bookmark.is_archived = True
