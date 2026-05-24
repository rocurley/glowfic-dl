import argparse
import os

import aiohttp
import aiolimiter
from ebooklib import epub

from .auth import login

from .helpers import make_filename_valid_for_epub3
from .render import (
    stylesheet,
    Continuity,
    ImageMap,
    Section,
    Thread,
    compile_chapters,
    download_chapters,
    generate_section_title_pages,
    generate_toc_and_spine,
    get_images_as_epub_items,
    get_book_structure,
)

# WIP on caching:
# The strategy of loading our cached data from the ebook is awkward. It would
# be a lot nicer if we instead cached the html so we could run the pipeline
# normally. Some challenges:
# * Image names are lost. Fixed by switching image names to their url's hash.
# * Thread IDs are lost. Fixed by switching book section names to be based on
#   the thread ID.
# * Link targets are lost. Unclear if this is important. We can't re-localize
#   links, but do we need to?
# * Images are loaded into the image map from the HTML soup, so cached pages
#   can't load images. CURRENTLY UNSOLVED
# * Different users (for the server use case) can see different versions of a
#   continuity (based on their permissions). Because continuity data is not
#   cached, we won't leak threads, but we cache data will be lost when a
#   lower-access user requests a continuity. CURRENTLY UNSOLVED (but will the
#   server even support multiple users?)
#

# How does link rewriting work?
# Internal links can be relative or absolute, so we need to account for that.
# They can also be to posts or replies. These need to be handled differently.
# Posts just need to go to the correct title page. Replies need to go to the section containing the relevant reply. Hilariously, they're written like this:
# https://glowfic.com/replies/2520772#reply-2520772

# TODO:
# * Better kobo handling
# * Rewrite internal links
#   Include linkbacks at the end of the thing that was linked to, eg: "This
#   post was linked to from reply #114 of Mad investor chaos". <a>Return
#   there</a>.
# * Less bad covers
# * Increase configurability of title page content


###################
##   Functions   ##
###################


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download glowfic from the Glowfic Constellation."
    )

    parser.add_argument("url", help="glowfic thread, section, or board URL")
    parser.add_argument(
        "-s",
        "--split",
        choices=["none", "if_large", "every_post"],
        default="if_large",
        help="how often (if at all) to split the output book's internal representations of threads into multiple files. 'none' means no splits occur except at thread boundaries; 'if_large' splits threads over 200kB in size after every 200kB; 'every_post' splits after each post irrespective of size. Default: if_large",
    )

    return parser.parse_args()


async def main():
    args = get_args()

    limiter = aiolimiter.AsyncLimiter(1, 1)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit_per_host=1)
    ) as slow_session:
        # Logging in prevents us from getting rate limited.
        # TODO: Some way to disable this and accept the rate limiting for users
        # without accounts.
        async with aiohttp.ClientSession() as fast_session:
            book_structure = await get_book_structure(slow_session, limiter, args.url)
            match book_structure:
                case Thread():
                    print("Found 1 thread")
                case Section():
                    print("Found %i threads" % len(book_structure.threads))
                case Continuity():
                    print(
                        "Found %i sections and %i threads"
                        % (len(book_structure.sections), len(book_structure.threads))
                    )

            out_path = make_filename_valid_for_epub3("%s.epub" % book_structure.title)
            try:
                old_book = epub.read_epub(out_path)
            except:
                old_book = None
            book = epub.EpubBook()
            image_map = ImageMap()
            if old_book:
                image_map.populate_from_book(old_book)
                book_structure.load_compiled_sections_from_old_book(old_book)

            await download_chapters(
                slow_session,
                limiter,
                fast_session,
                book_structure.threads,
                image_map,
                args.split,
            )
            compile_chapters(book_structure.threads)

            for thread in book_structure.threads:
                for section in thread.compiled_sections:
                    book.add_item(section)
            if isinstance(book_structure, Continuity):
                generate_section_title_pages(book_structure.sections)
                for section in book_structure.sections:
                    book.add_item(section.title_page)
            book.set_title(book_structure.title)

            style = epub.EpubItem(
                uid="style",
                file_name="style.css",
                media_type="text/css",
                content=stylesheet,
            )
            book.add_item(style)

            for image in get_images_as_epub_items(image_map):
                book.add_item(image)

            authors = set()
            for thread in book_structure.threads:
                for author in thread.authors:
                    authors.add(author)
            for author in sorted(authors):
                book.add_author(author)

            book.toc, book.spine = generate_toc_and_spine(book_structure)
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())

            print("Saving book to %s" % out_path)
            epub.write_epub(out_path, book, {})
