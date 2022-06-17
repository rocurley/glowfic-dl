import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import re
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import aiohttp
import aiolimiter
from bs4 import BeautifulSoup
from bs4.element import Tag, ResultSet
from ebooklib import epub
from lxml import etree
from tqdm.asyncio import tqdm

from .helpers import *


################
##   Consts   ##
################


SECTION_SIZE_LIMIT = 200000

RELATIVE_REPLY_RE = re.compile(r"/(replies|posts)/\d*")
ABSOLUTE_REPLY_RE = re.compile(
    r"https?://(www.)?glowfic.com(?P<relative>/(replies|posts)/\d*)"
)

GLOWFIC_ROOT = "https://glowfic.com"
GLOWFIC_TZ = ZoneInfo("America/New_York")


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
""".lstrip()

output_template = """
<html>
<head>
</head>
<body>
<div class="posts">
</div>
</body>
</html>
""".lstrip()


#################
##   Classes   ##
#################


class MappedImage:
    def __init__(self, name: str, id: int):
        self.name = name
        self.id = id
        self.downloaded = False
        self.is_null = False
        self.file = None
        self.media_type = None
        self.ext = None

    def add_file(self, file: Optional[bytes], url: str):
        self.downloaded = True
        if file is None:
            self.is_null = True
        processed = process_image_for_epub3(file)
        if processed is None:
            print("Downloaded %s, but it wasn't an image" % url)
            self.is_null = True
        else:
            self.file, self.media_type, self.ext = processed

    def get_filename(self, id_width: int) -> Optional[str]:
        if not self.downloaded:
            raise RuntimeError(
                "Attempted to get mapped image filename before getting it as a file. (This indicates a prior map population failure.)"
            )
        elif self.is_null:
            return None
        else:
            return "Images/%s%.*i.%s" % (self.name, id_width, self.id, self.ext)


class ImageMap:
    def __init__(self):
        self.icons = {}
        self.images = {}
        self.next_icon = 0
        self.icon_id_width = 1
        self.next_image = 0
        self.image_id_width = 1

    def add_icon(self, url: str):
        if url not in self.icons:
            self.icons[url] = MappedImage("icon", self.next_icon)
            self.icon_id_width = len(str(self.next_icon))
            self.next_icon += 1

    def get_icon_name(self, url: str) -> Optional[str]:
        if url not in self.icons:
            raise ValueError(
                "Attempted to get icon not in image map. (This indicates a prior map population failure.)"
            )
        return self.icons[url].get_filename(self.icon_id_width)

    def add_image(self, url: str):
        if url not in self.icons and url not in self.images:
            self.images[url] = MappedImage("image", self.next_image)
            self.image_id_width = len(str(self.next_icon))
            self.next_image += 1

    def get_image_name(self, url: str) -> Optional[str]:
        if url in self.icons:
            return self.get_icon_name(url)
        elif url not in self.images:
            raise ValueError(
                "Attempted to get image not in image map. (This indicates a prior map population failure.)"
            )
        else:
            return self.images[url].get_filename(self.image_id_width)


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


###################
##   Functions   ##
###################


def populate_image_map(posts: ResultSet, image_map: ImageMap):
    # Find icons
    for post in posts:
        icon = post.find("img", "icon")
        if icon:
            image_map.add_icon(icon["src"])

    # Find non-icon images
    for post in posts:
        for image in post.find("div", "post-content").find_all("img"):
            image_map.add_image(image["src"])


async def download_image(
    session: aiohttp.ClientSession, url: str, mapped_image: MappedImage
):
    try:
        async with session.get(url, timeout=15) as resp:
            file = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        print("Failed to download %s" % url)
        file = None
    mapped_image.add_file(file, url)


async def download_images(session: aiohttp.ClientSession, image_map: ImageMap):
    in_flight = []
    for (url, mapped_image) in image_map.icons.items():
        in_flight.append(download_image(session, url, mapped_image))
    for (url, mapped_image) in image_map.images.items():
        in_flight.append(download_image(session, url, mapped_image))
    await tqdm.gather(*in_flight)


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

    for inline_img in content.find_all("img"):
        mapped_image = image_map.get_image_name(inline_img["src"])
        if mapped_image is not None:
            inline_img["src"] = "../%s" % image_map.get_image_name(inline_img["src"])

    post_html = BeautifulSoup('<div class="post"></div>', "html.parser")
    post_div = post_html.find("div")
    permalink = post.find("img", title="Permalink", alt="Permalink").parent["href"]
    permalink_fragment = urlparse(permalink).fragment
    if permalink_fragment != "":
        reply_anchor = post_html.new_tag("a", id=permalink_fragment)
        post_div.extend([reply_anchor])  # for linking to this reply

    icon = post.find("img", "icon")
    if icon:
        mapped_icon = image_map.get_icon_name(icon["src"])
        if mapped_icon:
            local_image = BeautifulSoup('<img class="icon"></img>', "html.parser")
            local_image.find("img")["src"] = "../%s" % mapped_icon
            local_image.find("img")["alt"] = icon["alt"]
            post_div.extend([header, local_image] + content.contents)
        else:
            post_div.extend([header] + content.contents)
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
) -> Iterable[Section]:
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
) -> BeautifulSoup:
    await limiter.acquire()
    resp = await session.get(stamped_url.url, params={"view": "flat"})
    soup = BeautifulSoup(await resp.text(), "html.parser")
    resp.close()
    return soup


async def download_chapters(
    slow_session: aiohttp.ClientSession,
    limiter: aiolimiter.AsyncLimiter,
    fast_session: aiohttp.ClientSession,
    stamped_urls: list[StampedURL],
    image_map: ImageMap,
    authors: OrderedDict,
) -> list[tuple[str, list[Section]]]:
    print("Downloading chapter texts")
    chapter_soups = await tqdm.gather(
        *[
            download_chapter(slow_session, limiter, stamped_url)
            for stamped_url in stamped_urls
        ]
    )
    for chapter_soup in chapter_soups:
        posts = chapter_soup.find_all("div", "post-container")
        populate_image_map(posts, image_map)
    print("Downloading images")
    await download_images(fast_session, image_map)
    rendered_chapters = []
    for chapter_soup in chapter_soups:
        title = validate_tag(
            chapter_soup.find("span", id="post-title"), chapter_soup
        ).text.strip()
        rendered_chapters.append((title, list(render_posts(posts, image_map, authors))))
    return rendered_chapters


def compile_chapters(
    chapters: list[tuple[str, list[Section]]]
) -> Iterable[list[epub.EpubHtml]]:
    chapter_digits = len(str(len(chapters)))
    anchor_sections = {}

    # Map permalinks to file names
    for (i, (title, sections)) in enumerate(chapters):
        section_digits = len(str(len(sections) - 1))
        for (j, section) in enumerate(sections):
            file_name = make_filename_valid_for_epub3(
                "%.*i-%.*i (%s).xhtml"
                % (
                    chapter_digits,
                    i + 1,
                    section_digits,
                    j,
                    title,
                )
            )
            for permalink in section.link_targets:
                anchor_sections[permalink] = file_name

    # Replace external links with internal links where possible
    for (i, (title, sections)) in enumerate(chapters):
        for (j, section) in enumerate(sections):
            for a in section.html.find_all("a"):
                if "href" not in a.attrs:
                    continue
                raw_url = a["href"]
                url = urlparse(raw_url)
                if RELATIVE_REPLY_RE.match(raw_url) and raw_url in anchor_sections:
                    a["href"] = url._replace(path=anchor_sections[raw_url]).geturl()
                else:
                    abs = ABSOLUTE_REPLY_RE.match(raw_url)
                    if abs is not None and abs.group("relative") in anchor_sections:
                        a["href"] = anchor_sections[abs.group("relative")]
                    elif (
                        url.netloc == ""
                    ):  # Relative link to something not included here
                        a["href"] = url._replace(
                            scheme="https", netloc="glowfic.com"
                        ).geturl()

    # Yield one list of EpubHTML objects per chapter
    for (i, (title, sections)) in enumerate(chapters):
        section_digits = len(str(len(sections) - 1))
        compiled_sections = []
        for (j, section) in enumerate(sections):
            file_name = "Text/" + make_filename_valid_for_epub3(
                "%.*i-%.*i (%s).xhtml"
                % (
                    chapter_digits,
                    i + 1,
                    section_digits,
                    j,
                    title,
                )
            )
            compiled_section = epub.EpubHtml(
                title=title, file_name=file_name, media_type="application/xhtml+xml"
            )
            compiled_section.content = etree.tostring(
                etree.fromstring(
                    str(section.html), etree.XMLParser(remove_blank_text=True)
                ),
                encoding="unicode",
                pretty_print=True,
            )
            compiled_section.add_link(
                href="../style.css", rel="stylesheet", type="text/css"
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


def get_images_as_epub_items(image_map: ImageMap):
    items = []
    for url, icon in image_map.icons.items():
        filename = image_map.get_icon_name(url)
        if filename is None:
            continue
        items.append(
            epub.EpubItem(
                uid=filename,
                file_name=filename,
                media_type=icon.media_type,
                content=icon.file,
            )
        )
    for url, image in image_map.images.items():
        filename = image_map.get_image_name(url)
        if filename is None:
            continue
        items.append(
            epub.EpubItem(
                uid=filename,
                file_name=filename,
                media_type=image.media_type,
                content=image.file,
            )
        )
    return items
