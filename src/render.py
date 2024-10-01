import asyncio
from itertools import chain
import re
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
import aiolimiter
from bs4 import BeautifulSoup
from bs4.element import Tag, ResultSet
from ebooklib.epub import EpubHtml, EpubItem
from lxml import etree
from tqdm.asyncio import tqdm

from .helpers import make_filename_valid_for_epub3, process_image_for_epub3
from .auth import login
from .constants import GLOWFIC_ROOT


################
##   Consts   ##
################


SECTION_SIZE_LIMIT = 200000

RELATIVE_REPLY_RE = re.compile(r"/(replies|posts)/\d*")
ABSOLUTE_REPLY_RE = re.compile(
    r"https?://(www.)?glowfic.com(?P<relative>/(replies|posts)/\d*)"
)


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
.title, .authors, .description {
    text-align: center;
}
.extlink::after {
    content: "\u29c9";
    vertical-align: super;
	font-size: 0.7rem;
}
""".lstrip()

output_template = """
<html>
<head>
</head>
<body>
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
            return
        processed = process_image_for_epub3(file)
        if processed is None:
            print(
                "Downloaded %s, but it wasn't an image of EPUB3-compatible format or convertible thereto"
                % url
            )
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
        self.map = {}
        self.next_icon = 0
        self.icon_id_width = 1
        self.next_image = 0
        self.image_id_width = 1

    def add_icon(self, url: str):
        if url not in self.map:
            self.map[url] = MappedImage("icon", self.next_icon)
            self.icon_id_width = len(str(self.next_icon))
            self.next_icon += 1

    def add_image(self, url: str):
        if url not in self.map:
            self.map[url] = MappedImage("image", self.next_image)
            self.image_id_width = len(str(self.next_image))
            self.next_image += 1

    def get_icon_name(self, url: str) -> Optional[str]:
        if url not in self.map:
            raise ValueError(
                "Attempted to get icon not in image map. (This indicates a prior map population failure.)"
            )
        return self.map[url].get_filename(self.icon_id_width)

    def get_image_name(self, url: str) -> Optional[str]:
        if url not in self.map:
            raise ValueError(
                "Attempted to get image not in image map. (This indicates a prior map population failure.)"
            )
        return self.map[url].get_filename(self.image_id_width)


class RenderedPost:
    def __init__(
        self, html: BeautifulSoup, author: str, permalink: str, permalink_fragment: str
    ):
        self.html = html
        self.author = author
        self.permalink = permalink
        self.permalink_fragment = permalink_fragment


class HtmlSection:
    def __init__(self):
        self.html = BeautifulSoup(output_template, "html.parser")
        self.body = self.html.find("body")
        self.size = 0
        self.link_targets = []

    def append(self, post: RenderedPost):
        self.size += len(post.html.encode())
        self.body.append(post.html)
        self.link_targets.append(post.permalink)


class Thread:
    def __init__(self, title: str, url: str, description: Optional[str] = None):
        self.title = title
        self.url = url
        self.description = description

        self.soup = None
        self.rendered_sections = None
        self.compiled_sections = None

        self.threads = [self]

    def add_soup(self, soup: BeautifulSoup):
        self.soup = soup

    def add_rendered_sections(self, rendered_sections: list[HtmlSection]):
        self.rendered_sections = rendered_sections

    def add_compiled_sections(self, compiled_sections: list[EpubHtml]):
        self.compiled_sections = compiled_sections


class Section:
    def __init__(
        self,
        title: Optional[str],
        threads: list[Thread],
        description: Optional[str] = None,
    ):
        self.title = title
        self.threads = threads
        self.description = description

        self.title_page = None

    def add_title_page(self, title_page: EpubHtml):
        self.title_page = title_page


class Continuity:
    def __init__(
        self,
        title: str,
        sections: list[Section],
        sectionless_threads: Optional[Section] = None,
    ):
        self.title = title
        self.sections = sections
        self.sectionless_threads = sectionless_threads

        self.title_page = None

        self.threads = list(chain(*[section.threads for section in self.sections]))
        if sectionless_threads is not None:
            self.threads += sectionless_threads.threads

    def add_title_page(self, title_page: HtmlSection):
        self.title_page = title_page


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
            inline_img["src"] = "../%s" % mapped_image
        else:
            inline_img["src"] = "data:,"

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
    posts: ResultSet, image_map: ImageMap, authors: set, title: str, split: str
) -> Iterable[HtmlSection]:
    rendered_posts = [render_post(post, image_map) for post in posts]

    # Thread title page
    thread_authors = set()
    for post in rendered_posts:
        thread_authors.add(post.author)
    authors.update(thread_authors)

    title_page = HtmlSection()
    title_page.body.extend(
        BeautifulSoup('<h2 class="title">%s</h2>' % title, "html.parser")
    )
    title_page.body.extend(
        BeautifulSoup(
            '<h3 class="authors">%s</h2>' % ", ".join(sorted(thread_authors)),
            "html.parser",
        )
    )
    yield title_page

    # Thread posts
    current_section = HtmlSection()
    for post in rendered_posts:
        post_size = len(post.html.encode())
        if (
            split == "if_large"
            and current_section.size + post_size > SECTION_SIZE_LIMIT
            and current_section.size > 0
        ):
            yield current_section
            current_section = HtmlSection()
        current_section.append(post)
        if split == "every_post":
            yield current_section
            current_section = HtmlSection()
    if current_section.size > 0:
        yield current_section


async def download_chapter(
    session: aiohttp.ClientSession,
    limiter: aiolimiter.AsyncLimiter,
    thread: Thread,
):
    await limiter.acquire()
    resp = await session.get(thread.url, params={"view": "flat"})
    soup = BeautifulSoup(await resp.text(), "html.parser")
    resp.close()
    thread.add_soup(soup)


async def download_chapters(
    slow_session: aiohttp.ClientSession,
    limiter: aiolimiter.AsyncLimiter,
    fast_session: aiohttp.ClientSession,
    threads: list[Thread],
    image_map: ImageMap,
    authors: set,
    split: str,
):
    print("Downloading chapter texts")
    await tqdm.gather(
        *[download_chapter(slow_session, limiter, thread) for thread in threads]
    )
    for thread in threads:
        posts = thread.soup.find_all("div", "post-container")
        populate_image_map(posts, image_map)
    print("Downloading images")
    await tqdm.gather(
        *[
            download_image(fast_session, url, mapped_image)
            for (url, mapped_image) in image_map.map.items()
        ]
    )
    for thread in threads:
        posts = thread.soup.find_all("div", "post-container")
        thread.add_rendered_sections(
            list(render_posts(posts, image_map, authors, thread.title, split))
        )


def map_permalinks_to_filenames(
    threads: list[Thread], chapter_digits: int
) -> dict[str, str]:
    anchor_sections = {}
    for i, thread in enumerate(threads):
        section_digits = len(str(len(thread.rendered_sections) - 1))
        for (j, section) in enumerate(thread.rendered_sections):
            file_name = make_filename_valid_for_epub3(
                "%.*i-%.*i (%s).xhtml"
                % (
                    chapter_digits,
                    i + 1,
                    section_digits,
                    j,
                    thread.title,
                )
            )
            for permalink in section.link_targets:
                anchor_sections[permalink] = file_name
    return anchor_sections


def replace_or_tag_external_links_from_sections(
    threads: list[Thread], chapter_digits: int
):
    anchor_sections = map_permalinks_to_filenames(threads, chapter_digits)
    for thread in threads:
        for section in thread.rendered_sections:
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
                    else:  # External link
                        a["class"] = a.get("class", []) + ["extlink"]
                        if url.netloc == "":
                            a["href"] = url._replace(
                                scheme="https", netloc="glowfic.com"
                            ).geturl()


def compile_sections(threads: list[Thread], chapter_digits: int):
    for i, thread in enumerate(threads):
        section_digits = len(str(len(thread.rendered_sections) - 1))
        compiled_sections = []
        for j, section in enumerate(thread.rendered_sections):
            file_name = "Text/" + make_filename_valid_for_epub3(
                "%.*i-%.*i (%s).xhtml"
                % (
                    chapter_digits,
                    i + 1,
                    section_digits,
                    j,
                    thread.title,
                )
            )
            compiled_section = EpubHtml(
                title=thread.title,
                file_name=file_name,
                media_type="application/xhtml+xml",
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
        thread.add_compiled_sections(compiled_sections)


def compile_chapters(threads: list[Thread]) -> Iterable[list[EpubHtml]]:
    chapter_digits = len(str(len(threads)))
    replace_or_tag_external_links_from_sections(threads, chapter_digits)
    compile_sections(threads, chapter_digits)


def generate_section_title_pages(sections: list[Section]):
    section_digits = len(str(len(sections)))
    for i, section in enumerate(sections):
        title_page = HtmlSection()
        title_page.body.extend(
            BeautifulSoup('<h1 class="title">%s</h1>' % section.title, "html.parser")
        )
        if section.description is not None:
            title_page.body.extend(
                BeautifulSoup(
                    '<h3 class="description">%s</h2>' % section.description,
                    "html.parser",
                )
            )
        file_name = "Text/" + make_filename_valid_for_epub3(
            "section%.*i (%s).xhtml" % (section_digits, i + 1, section.title)
        )
        compiled_title_page = EpubHtml(
            title=section.title, file_name=file_name, media_type="application/xhtml+xml"
        )
        compiled_title_page.content = etree.tostring(
            etree.fromstring(
                str(title_page.html), etree.XMLParser(remove_blank_text=True)
            ),
            encoding="unicode",
            pretty_print=True,
        )
        compiled_title_page.add_link(
            href="../style.css", rel="stylesheet", type="text/css"
        )
        section.add_title_page(compiled_title_page)


def generate_toc_and_spine(
    book_structure: Thread | Section | Continuity,
) -> tuple[list[EpubHtml | list[EpubHtml | list[EpubHtml]]], list[str | EpubHtml]]:
    spine = ["nav"]
    match book_structure:
        case Thread():
            toc = [book_structure.compiled_sections[0]]
            spine += book_structure.compiled_sections
        case Section():
            toc = [thread.compiled_sections[0] for thread in book_structure.threads]
            spine += list(
                chain(*[thread.compiled_sections for thread in book_structure.threads])
            )
        case Continuity():
            toc = []
            for section in book_structure.sections:
                toc.append(
                    [
                        section.title_page,
                        [thread.compiled_sections[0] for thread in section.threads],
                    ]
                )
                spine += [section.title_page] + list(
                    chain(*[thread.compiled_sections for thread in section.threads])
                )
            if book_structure.sectionless_threads is not None:
                toc += [
                    thread.compiled_sections[0]
                    for thread in book_structure.sectionless_threads.threads
                ]
                spine += list(
                    chain(
                        *[
                            thread.compiled_sections
                            for thread in book_structure.sectionless_threads.threads
                        ]
                    )
                )
    return toc, spine


def validate_tag(tag: Tag, soup: BeautifulSoup) -> Tag:
    if tag is not None:
        return tag
    err = soup.find("div", "flash error")
    if err is not None:
        raise RuntimeError(err.text.strip())
    else:
        raise RuntimeError("Unknown error: tag missing")


def thread_from_board_row(row: Tag) -> Thread:
    thread_link = row.find("a")
    title = thread_link.text.strip()
    description = thread_link.get("title")
    url = urljoin(GLOWFIC_ROOT, thread_link["href"])
    return Thread(title, url, description)


def sections_from_board_rows(rows: ResultSet) -> Iterable[Section]:
    current_title = None
    current_threads = []
    current_description = None

    for row in rows:
        if (title := row.find("th", "continuity-header")) is not None:
            current_title = next(title.children).text.strip()
        elif (description := row.find("td", "written-content")) is not None:
            current_description = description.text.strip()
        elif (thread := row.find("td", "post-subject")) is not None:
            current_threads.append(thread_from_board_row(thread))
        elif row.find("td", "continuity-spacer") is not None:
            if len(current_threads) == 0:
                current_title = None
                current_description = None
                continue
            elif current_title is not None:
                yield Section(current_title, current_threads, current_description)
                current_title = None
                current_threads = []
                current_description = None
            else:
                raise Exception(
                    "Encountered nonfinal titleless section. (This should be impossible.)"
                )

    if len(current_threads) > 0:
        yield Section(current_title, current_threads, current_description)


async def get_book_structure(
    session: aiohttp.ClientSession, limiter: aiolimiter.AsyncLimiter, url: str
) -> Thread | Section | Continuity:
    target_url = (
        "https://glowfic.com/api/v1%s" % urlparse(url).path if "posts" in url else url
    )
    await limiter.acquire()
    resp = await session.get(target_url)
    if resp.status == 403:
        await login(session)
        resp = await session.get(target_url)
        assert resp.status != 403

    if "posts" in url:
        post_json = await resp.json()
        return Thread(post_json["subject"], url, post_json.get("description"))
    elif "board_sections" in url:
        soup = BeautifulSoup(await resp.text(), "html.parser")
        title = soup.find("th", "table-title").text.strip()
        description = soup.find("td", "written-content")
        if description is not None:
            description = description.text.strip()
        rows = validate_tag(soup.find("div", id="content"), soup).find_all(
            "td", "post-subject"
        )
        threads = [thread_from_board_row(row) for row in rows]
        return Section(title, threads, description)
    elif "boards" in url:
        soup = BeautifulSoup(await resp.text(), "html.parser")
        title = next(soup.find("th", "table-title").children).strip()
        rows = validate_tag(soup.find("div", id="content"), soup).find_all("tr")
        sections = list(sections_from_board_rows(rows))
        if sections[-1].title is None:
            return Continuity(title, sections[:-1], sections[-1])
        else:
            return Continuity(title, sections)
    else:
        raise ValueError(
            "URL contains neither 'posts' nor 'board_sections' nor 'boards'."
        )


def get_images_as_epub_items(image_map: ImageMap) -> list[EpubItem]:
    items = []
    for url, mapped_image in image_map.map.items():
        match mapped_image.name:
            case "icon":
                filename = image_map.get_icon_name(url)
            case "image":
                filename = image_map.get_image_name(url)
            case _:
                raise ValueError("Mapped image name is neither 'icon' nor 'image'.")
        if filename is None:
            continue
        items.append(
            EpubItem(
                uid=filename,
                file_name=filename,
                media_type=mapped_image.media_type,
                content=mapped_image.file,
            )
        )
    return items
