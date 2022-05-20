#!/usr/bin/env python3

import argparse
import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import os
import platform
import re
from typing import Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import aiohttp
import aiolimiter
from bs4 import BeautifulSoup
from bs4.element import Tag, ResultSet
from ebooklib import epub
from tqdm.asyncio import tqdm

# TODO:
# * Better kobo handling
# * Rewrite internal links
#   Include linkbacks at the end of the thing that was linked to, eg: "This
#   post was linked to from reply #114 of Mad investor chaos". <a>Return
#   there</a>.
# * Less bad covers


#################
##   Globals   ##
#################


SECTION_SIZE_LIMIT = 200000

REPLY_RE = re.compile(r"/(replies|posts)/\d*")

GLOWFIC_ROOT = "https://glowfic.com"
GLOWFIC_TZ = ZoneInfo("America/New_York")

COOKIE_NAME = "_glowfic_constellation_production"


###################
##   Templates   ##
###################


stylesheet = """
img.icon {
    width:100px;
    float:left;
    margin-right: 1em;
    margin-bottom: 1em;
}
div.post {
    overflow: hidden;
    padding: 0.5em;
    border: solid grey 0.5em;
    page-break-inside: avoid;
}
div.post + div.post {
    margin-top: -0.5em;
}
""".strip()

output_template = """
<html>
<head>
</head>
<body>
<div class="posts">
</div>
</body>
</html>
""".strip()


#################
##   Classes   ##
#################


class ImageMap:
    def __init__(self):
        self.map = {}
        self.next = 0

    def insert(self, url: str):
        if url not in self.map:
            path = urlparse(url).path
            ext = path.split(".")[-1]
            self.map[url] = "img%i.%s" % (self.next, ext)
            self.next += 1
        return self.map[url]


class RenderedPost:
    def __init__(
        self, html: BeautifulSoup, author: str, permalink: str, permalink_fragment: str
    ):
        self.html = html
        self.author = author
        self.permalink = permalink
        self.permalink_fragment = permalink_fragment


class Section:
    def __init__(self):
        self.html = BeautifulSoup(output_template, "html.parser")
        self.body = self.html.find("div")
        self.size = 0
        self.link_targets = []

    def append(self, post: RenderedPost):
        post_size = len(post.html.encode())
        self.size += post_size
        self.body.append(post.html)
        self.link_targets.append(post.permalink)


class StampedURL:
    def __init__(self, url: str, stamp: datetime):
        self.url = url
        self.stamp = stamp


class BookSpec:
    def __init__(self, stamped_urls: list[StampedURL], title: str):
        self.stamped_urls = stamped_urls
        self.title = title
        self.last_update = max((stamped_url.stamp for stamped_url in stamped_urls))


###########################
##   Scrape and Render   ##
###########################


def render_post(post: Tag, image_map: ImageMap) -> RenderedPost:
    try:
        character = post.find("div", "post-character").text.strip()
    except AttributeError:
        character = None
    try:
        screen_name = post.find("div", "post-screenname").text.strip()
    except AttributeError:
        screen_name = None
    try:
        author = post.find("div", "post-author").text.strip()
    except AttributeError:
        author = None
    content = post.find("div", "post-content")
    header = BeautifulSoup("<p><strong></strong></p>", "html.parser")
    header.find("strong").string = " / ".join(
        [x for x in [character, screen_name, author] if x is not None]
    )

    post_html = BeautifulSoup('<div class="post"></div>', "html.parser")
    post_div = post_html.find("div")
    permalink = post.find("img", title="Permalink", alt="Permalink").parent["href"]
    permalink_fragment = urlparse(permalink).fragment
    reply_anchor = post_html.new_tag("a", id=permalink_fragment)
    post_div.extend([reply_anchor])  # for linking to this reply

    image = post.find("img", "icon")
    if image:
        local_image = BeautifulSoup('<img class="icon"></img>', "html.parser")
        local_image.find("img")["src"] = image_map.insert(image["src"])
        post_div.extend([header, local_image] + content.contents)
    else:
        post_div.extend([header] + content.contents)
    return RenderedPost(
        html=post_html,
        author=author,
        permalink=permalink,
        permalink_fragment=permalink_fragment,
    )


def render_posts(
    posts: ResultSet, image_map: ImageMap, authors: OrderedDict
) -> Section:
    out = Section()
    for post in posts:
        rendered = render_post(post, image_map)
        post_size = len(rendered.html.encode())
        if out.size + post_size > SECTION_SIZE_LIMIT and out.size > 0:
            yield out
            out = Section()
        out.append(rendered)
        authors[rendered.author] = True
    yield out


async def download_chapter(
    session: aiohttp.ClientSession,
    limiter: aiolimiter.AsyncLimiter,
    stamped_url: StampedURL,
    image_map: ImageMap,
    authors: OrderedDict,
) -> tuple[str, list[Section]]:
    await limiter.acquire()
    resp = await session.get(stamped_url.url, params={"view": "flat"})
    soup = BeautifulSoup(await resp.text(), "html.parser")
    resp.close()
    posts = soup.find_all("div", "post-container")
    title = validate_tag(soup.find("span", id="post-title"), soup).text.strip()
    return (title, list(render_posts(posts, image_map, authors)))


def compile_chapters(chapters: list[tuple[str, list[Section]]]) -> list[epub.EpubHtml]:
    anchor_sections = {}
    for (i, (title, sections)) in enumerate(chapters):
        for (j, section) in enumerate(sections):
            file_name = "chapter%i_%i.html" % (i, j)
            for permalink in section.link_targets:
                anchor_sections[permalink] = file_name
    for (i, (title, sections)) in enumerate(chapters):
        for (j, section) in enumerate(sections):
            for a in section.html.find_all("a"):
                if "href" not in a.attrs:
                    continue
                raw_url = a["href"]
                url = urlparse(raw_url)
                if REPLY_RE.match(raw_url) and raw_url in anchor_sections:
                    a["href"] = url._replace(path=anchor_sections[raw_url]).geturl()
                elif url.netloc == "":  # Glowfic link to something not included here
                    a["href"] = url._replace(
                        scheme="https", netloc="glowfic.com"
                    ).geturl()
    for (i, (title, sections)) in enumerate(chapters):
        compiled_sections = []
        for (j, section) in enumerate(sections):
            file_name = "chapter%i_%i.html" % (i, j)
            compiled_section = epub.EpubHtml(
                title=title, file_name=file_name, media_type="application/xhtml+xml"
            )
            compiled_section.content = str(section.html)
            compiled_section.add_link(
                href="style.css", rel="stylesheet", type="text/css"
            )
            compiled_sections.append(compiled_section)
        yield compiled_sections


def validate_tag(tag: Tag, soup: BeautifulSoup) -> Tag:
    if tag is not None:
        return tag
    err = soup.find("div", "flash error")
    if err is not None:
        raise RuntimeError(err.text.strip())
    else:
        raise RuntimeError("Unknown error: tag missing")


def stamped_url_from_board_row(row: Tag) -> StampedURL:
    url = urljoin(GLOWFIC_ROOT, row.find("a")["href"])
    ts_raw = (
        next(row.parent.find("td", class_="post-time").strings).split("by")[0].strip()
    )
    ts_local = datetime.strptime(ts_raw, "%b %d, %Y  %I:%M %p").replace(
        tzinfo=GLOWFIC_TZ
    )
    ts = ts_local.astimezone(timezone.utc)
    return StampedURL(url, ts)


async def get_post_urls_and_title(
    session: aiohttp.ClientSession, limiter: aiolimiter.AsyncLimiter, url: str
) -> BookSpec:
    if "posts" in url:
        api_url = "https://glowfic.com/api/v1%s" % urlparse(url).path
        await limiter.acquire()
        resp = await session.get(api_url)
        post_json = await resp.json()
        ts = datetime.strptime(post_json["tagged_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
        title = post_json["subject"]
        return BookSpec(stamped_urls=[StampedURL(url, ts)], title=title)
    if "board_sections" in url or "boards" in url:
        await limiter.acquire()
        resp = await session.get(url)
        soup = BeautifulSoup(await resp.text(), "html.parser")
        rows = validate_tag(soup.find("div", id="content"), soup).find_all(
            "td", "post-subject"
        )
        stamped_urls = [stamped_url_from_board_row(row) for row in rows]
        title = soup.find("th", "table-title").contents[0].strip()
        return BookSpec(title=title, stamped_urls=stamped_urls)


async def download_image(
    session: aiohttp.ClientSession, url: str, id: str
) -> Optional[epub.EpubItem]:
    try:
        async with session.get(url, timeout=15) as resp:
            item = epub.EpubItem(
                uid=id,
                file_name=id,
                media_type=resp.headers["Content-Type"],
                content=await resp.read(),
            )
            return item
    except (aiohttp.ClientError, asyncio.TimeoutError):
        print("Failed to download %s" % url)
        return None


async def download_images(
    session: aiohttp.ClientSession, image_map: ImageMap
) -> list[epub.EpubItem]:
    in_flight = []
    for (k, v) in image_map.map.items():
        in_flight.append(download_image(session, k, v))
    return [image for image in await tqdm.gather(*in_flight) if image is not None]


##############
##   Main   ##
##############


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download glowfic from the Glowfic Constellation."
    )

    parser.add_argument("url", help = "glowfic post, section, or board URL")

    return parser.parse_args()


def get_cookies() -> dict[str, str]:
    cookies = {}

    if os.path.exists("cookie"):
        with open("cookie", "r") as fin:
            raw = fin.read()
            (name, cookie) = raw.split("=")
            if name != COOKIE_NAME:
                raise ValueError(
                    f'cookie file must start with "{COOKIE_NAME}=" (no quotes)'
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


if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(main())
