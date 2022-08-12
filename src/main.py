import argparse
import os

import aiohttp
import aiolimiter
from ebooklib import epub

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

# TODO:
# * Better kobo handling
# * Rewrite internal links
#   Include linkbacks at the end of the thing that was linked to, eg: "This
#   post was linked to from reply #114 of Mad investor chaos". <a>Return
#   there</a>.
# * Less bad covers
# * Increase configurability of title page content


################
##   Consts   ##
################


COOKIE_NAME = "_glowfic_constellation_production"


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


def get_cookies() -> dict[str, str]:
    cookies = {}

    if os.path.exists("cookie"):
        with open("cookie", "r") as fin:
            raw = fin.read()
            (name, cookie) = raw.split("=")
            if name != COOKIE_NAME:
                raise ValueError(
                    'cookie file must start with "%s=" (no quotes)' % COOKIE_NAME
                )
            cookies[COOKIE_NAME] = cookie.strip()

    return cookies


async def main():
    args = get_args()
    cookies = get_cookies()

    limiter = aiolimiter.AsyncLimiter(1, 1)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit_per_host=1), cookies=cookies
    ) as slow_session:
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

            book = epub.EpubBook()
            image_map = ImageMap()
            authors = set()

            await download_chapters(
                slow_session,
                limiter,
                fast_session,
                book_structure.threads,
                image_map,
                authors,
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

            for author in sorted(authors):
                book.add_author(author)

            book.toc, book.spine = generate_toc_and_spine(book_structure)
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())

            out_path = "%s.epub" % book_structure.title
            print("Saving book to %s" % out_path)
            epub.write_epub(out_path, book, {})
