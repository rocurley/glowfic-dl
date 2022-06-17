import argparse
from collections import OrderedDict
import os

import aiohttp
import aiolimiter
from ebooklib import epub
from tqdm.asyncio import tqdm

from .render import *

# TODO:
# * Better kobo handling
# * Rewrite internal links
#   Include linkbacks at the end of the thing that was linked to, eg: "This
#   post was linked to from reply #114 of Mad investor chaos". <a>Return
#   there</a>.
# * Less bad covers
# * When taking in images of format other than PNG/JPEG/GIF/SVG, convert them
#   to one of those formats, for EPUB specification compliance
# * Download inline images too, not just icons
# * Fix whatever's wrong with link-rewriting right now


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

    parser.add_argument("url", help="glowfic post, section, or board URL")

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
            spec = await get_post_urls_and_title(slow_session, limiter, args.url)
            print("Found %i chapters" % len(spec.stamped_urls))

            book = epub.EpubBook()
            image_map = ImageMap()
            authors = OrderedDict()

            print("Downloading chapter texts")
            chapters = await tqdm.gather(
                *[
                    download_chapter(
                        slow_session, limiter, stamped_url, image_map, authors
                    )
                    for (i, stamped_url) in enumerate(spec.stamped_urls)
                ]
            )
            chapters = list(compile_chapters(chapters))
            for chapter in chapters:
                for section in chapter:
                    book.add_item(section)
            book.set_title(spec.title)

            style = epub.EpubItem(
                uid="style",
                file_name="style.css",
                media_type="text/css",
                content=stylesheet,
            )
            book.add_item(style)

            print("Downloading images")
            images = await download_images(fast_session, image_map)

            for image in images:
                book.add_item(image)

            for author in authors.keys():
                book.add_author(author)

            book.toc = [chapter[0] for chapter in chapters]
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())

            book.spine = ["nav"] + [
                section for chapter in chapters for section in chapter
            ]

            out_path = "%s.epub" % spec.title
            print("Saving book to %s" % out_path)
            epub.write_epub(out_path, book, {})
