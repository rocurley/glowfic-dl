import asyncio
from datetime import datetime
from itertools import chain
import itertools
from pathlib import Path
import re
from typing import Any, Iterable, Optional, Union
import ebooklib
from typing_extensions import Self
from urllib.parse import urlparse
from hashlib import sha256

import aiohttp
import aiolimiter
from dataclasses import dataclass
from bs4 import BeautifulSoup
from bs4.element import AttributeValueList, Tag, ResultSet
from ebooklib.epub import EpubHtml, EpubItem, EpubBook
from lxml import etree
from tqdm.asyncio import tqdm

from .helpers import get_attr, make_filename_valid_for_epub3, process_image_for_epub3
from .auth import auth_get, login


################
##   Consts   ##
################


SECTION_SIZE_LIMIT = 200000

RELATIVE_REPLY_RE = re.compile(r"/(replies|posts)/\d*")
ABSOLUTE_REPLY_RE = re.compile(
    r"https?://(www.)?glowfic.com(?P<relative>/(replies|posts)/\d*)"
)
COMPILED_REPLY_RE = re.compile(r"reply-(\d*)")


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


@dataclass
class Uninitialized:
    pass


@dataclass
class Failed:
    pass


@dataclass
class Succeeded:
    file: bytes
    media_type: str
    ext: str


ImageData = Union[Uninitialized, Failed, Succeeded]


def parse_image_filename(filename: Path) -> tuple[str, str, str]:
    [ty, hash] = filename.stem.split("_")
    ext = filename.suffix.strip(".")
    return (ty, hash, ext)


class MappedImage:
    def __init__(self, type: str, hash: str):
        self.type = type
        self.hash: str = hash
        self.data: ImageData = Uninitialized()

    def add_file(self, file: Optional[bytes], url: str):
        if file is None:
            self.data = Failed()
            return
        processed = process_image_for_epub3(file)
        if processed is None:
            print(
                "Downloaded %s, but it wasn't an image of EPUB3-compatible format or convertible thereto"
                % url
            )
            self.data = Failed()
        else:
            file, media_type, ext = processed
            self.data = Succeeded(file=file, media_type=media_type, ext=ext)

    def get_filename(self) -> Optional[str]:
        match self.data:
            case Uninitialized():
                raise RuntimeError(
                    "Attempted to get mapped image filename before getting it as a file. (This indicates a prior map population failure.)"
                )
            case Failed():
                return None
            case Succeeded(ext=ext):
                return "Images/%s_%s.%s" % (self.type, self.hash, ext)

    @classmethod
    def from_file(cls, filename: Path, file: bytes) -> Self:
        (ty, hash, expected_ext) = parse_image_filename(filename)
        out = cls(ty, hash)
        match process_image_for_epub3(file):
            case None:
                raise Exception("Unexpectedly invalid image found in cached epub")
            case [file, media_type, ext]:
                assert ext == expected_ext
                out.data = Succeeded(file=file, media_type=media_type, ext=ext)
                return out


class ImageMap:
    def __init__(self):
        self.map: dict[str, MappedImage] = {}  # url -> image
        self.cached_images: dict[str, MappedImage] = {}  # hash -> image
        self.cached_posts_images: set[str] = set()  # by hash

    def add_icon(self, url: str):
        self._add_image_untyped(url, "icon")

    def add_image(self, url: str):
        self._add_image_untyped(url, "image")

    def _add_image_untyped(self, url: str, ty: str):
        if url in self.map:
            return
        hash = sha256(url.encode("utf-8")).hexdigest()
        image = self.cached_images.get(hash)
        if image is None:
            image = MappedImage(ty, hash)
        else:
            assert image.type == ty
            del self.cached_images[hash]
        self.map[url] = image

    def add_cached_image(self, filename: Path, file: bytes):
        image = MappedImage.from_file(filename, file)
        self.cached_images[image.hash] = image

    # tracks which cached images have been used from cached text
    def add_cached_image_usage(self, path: str):
        (_, hash, _) = parse_image_filename(Path(path))
        self.cached_posts_images.add(hash)

    def get_icon_name(self, url: str) -> Optional[str]:
        if url not in self.map:
            raise ValueError(
                "Attempted to get icon not in image map. (This indicates a prior map population failure.)"
            )
        return self.map[url].get_filename()

    def get_image_name(self, url: str) -> Optional[str]:
        if url not in self.map:
            raise ValueError(
                "Attempted to get image not in image map. (This indicates a prior map population failure.)"
            )
        return self.map[url].get_filename()

    def populate_from_book(self, book: EpubBook):
        for image in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            self.add_cached_image(Path(image.get_name()), image.get_content())


class RenderedPost:
    def __init__(self, html: BeautifulSoup, permalink: str, permalink_fragment: str):
        self.html = html
        self.permalink = permalink
        self.permalink_fragment = permalink_fragment


class HtmlSection:
    def __init__(self):
        self.html = BeautifulSoup(output_template, "html.parser")
        body = self.html.find("body")
        assert body is not None
        self.body = body
        self.size = 0
        self.link_targets = []

    def append(self, post: RenderedPost):
        self.size += len(post.html.encode())
        self.body.append(post.html)
        self.link_targets.append(post.permalink)


class Thread:
    def __init__(self, post_json):
        self.id: int = post_json["id"]
        self.title: str = post_json["subject"]
        self.url: str = "https://glowfic.com/posts/%d" % post_json["id"]
        self.updated_at: datetime = datetime.fromisoformat(
            post_json["tagged_at"].strip("Z")
        )
        self.description: str = post_json.get("description")
        self.authors = [author["username"] for author in post_json["authors"]]

        self.soup: BeautifulSoup | None = None
        self.rendered_sections: list[HtmlSection] | None = None
        self.compiled_sections: list[EpubHtml] | None = None

        self.threads: list[Thread] = [self]

    def add_soup(self, soup: BeautifulSoup):
        self.soup = soup

    def add_rendered_sections(self, rendered_sections: list[HtmlSection]):
        self.rendered_sections = rendered_sections

    def add_compiled_sections(self, compiled_sections: list[EpubHtml]):
        self.compiled_sections = compiled_sections

    def section_name(self, section_ix: int) -> str:
        sections = self.compiled_sections or self.rendered_sections
        assert sections is not None
        section_digits = len(str(len(sections) - 1))
        return make_filename_valid_for_epub3(
            "%i-%.*i.xhtml"
            % (
                self.id,
                section_digits,
                section_ix,
            )
        )

    def load_compiled_sections_from_old_book(self, old_book: EpubBook):
        old_version_ts = last_modified(old_book)
        if old_version_ts < self.updated_at:
            return
        compiled_sections = []
        for item in old_book.get_items():
            item.id = None
            if item.get_name().startswith(f"Text/{self.id}"):
                # Unclear why these are lost
                item.title = self.title
                item.add_meta(name="glowfic-post-id", content=str(self.id))
                item.add_link(href="../style.css", rel="stylesheet", type="text/css")
                compiled_sections.append(item)
        # Can happen if the re-run with more permissions
        if len(compiled_sections) == 0:
            return
        compiled_sections.sort(key=EpubHtml.get_name)
        self.compiled_sections = compiled_sections


def last_modified(book: EpubBook) -> datetime:
    for (content, attrs) in book.get_metadata("OPF", "meta"):
        if attrs.get("property") == "dcterms:modified":
            return datetime.fromisoformat(content.strip("Z"))
    raise Exception("Couldn't find dcterms:modified")


class Section:
    def __init__(
        self,
        id: int | None,
        title: Optional[str],
        threads: list[Thread],
        description: Optional[str] = None,
    ):
        self.id = id
        self.title = title
        self.threads = threads
        self.description = description

        self.title_page = None

    @classmethod
    def from_jsons(cls, id: int | None, name: str | None, post_jsons) -> Self:
        post_jsons.sort(key=lambda j: j["section_order"])
        threads = [Thread(post_json) for post_json in post_jsons]
        return cls(id=id, title=name, threads=threads)

    def add_title_page(self, title_page: EpubHtml):
        self.title_page = title_page

    def load_compiled_sections_from_old_book(self, old_book: EpubBook):
        for thread in self.threads:
            thread.load_compiled_sections_from_old_book(old_book)


class Continuity:
    def __init__(
        self,
        id: int,
        title: str,
        sections: list[Section],
        sectionless_threads: Optional[Section] = None,
    ):
        self.id = id
        self.title = title
        self.sections = sections
        self.sectionless_threads = sectionless_threads

        self.title_page = None

        self.threads = list(chain(*[section.threads for section in self.sections]))
        if sectionless_threads is not None:
            self.threads += sectionless_threads.threads

    def add_title_page(self, title_page: HtmlSection):
        self.title_page = title_page

    def load_compiled_sections_from_old_book(self, old_book: EpubBook):
        for thread in self.threads:
            thread.load_compiled_sections_from_old_book(old_book)


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
    if isinstance(mapped_image.data, Succeeded):
        return
    try:
        headers = {}
        if "imgur" in url:
            headers = {"user-agent": "curl/8.1.1", "accept": "*/*"}
        async with session.get(url, timeout=15, headers=headers) as resp:
            file = await resp.read()
            if len(file) == 0:
                print("Empty download for %s" % url)
                file = None

    except (aiohttp.ClientError, asyncio.TimeoutError):
        print("Failed to download %s" % url)
        file = None
    mapped_image.add_file(file, url)


def render_post(post: Tag, image_map: ImageMap) -> RenderedPost:
    match post.find("div", class_="post-character"):
        case None:
            character = None
        case tag:
            character = tag.text.strip()
    match post.find("div", class_="post-screenname"):
        case None:
            screen_name = None
        case tag:
            screen_name = tag.text.strip()
    match post.find("div", class_="post-author"):
        case None:
            author = None
        case tag:
            author = tag.text.strip()
    content = post.find("div", class_="post-content")
    assert content is not None
    header = BeautifulSoup("<p><strong></strong></p>", "html.parser")
    character_name = header.find("strong")
    assert character_name is not None
    character_name.string = " / ".join(
        [x for x in [character, screen_name, author] if x is not None]
    )

    for inline_img in content.find_all("img"):
        mapped_image = image_map.get_image_name(get_attr(inline_img, "src"))
        if mapped_image is not None:
            inline_img["src"] = "../%s" % mapped_image
        else:
            inline_img["src"] = "data:,"

    post_html = BeautifulSoup('<div class="post"></div>', "html.parser")
    post_div = post_html.find("div")
    assert post_div is not None
    permalink_img = post.find("img", title="Permalink", alt="Permalink")
    assert permalink_img is not None
    assert permalink_img.parent is not None
    permalink = get_attr(permalink_img.parent, "href")
    permalink_fragment = urlparse(permalink).fragment
    if permalink_fragment != "":
        reply_anchor = post_html.new_tag("a", id=permalink_fragment)
        post_div.extend([reply_anchor])  # for linking to this reply

    icon = post.find("img", class_="icon")
    if icon:
        mapped_icon = image_map.get_icon_name(get_attr(icon, "src"))
        if mapped_icon:
            local_image_html = BeautifulSoup('<img class="icon"></img>', "html.parser")
            local_image = local_image_html.find("img")
            assert local_image is not None
            local_image["src"] = "../%s" % mapped_icon
            local_image["alt"] = icon["alt"]
            post_div.extend([header, local_image_html] + content.contents)
        else:
            post_div.extend([header] + content.contents)
    else:
        post_div.extend([header] + content.contents)
    return RenderedPost(
        html=post_html,
        permalink=permalink,
        permalink_fragment=permalink_fragment,
    )


def render_posts(
    posts: Iterable[Tag],
    image_map: ImageMap,
    authors: list[str],
    title: str,
    split: str,
) -> Iterable[HtmlSection]:
    rendered_posts = [render_post(post, image_map) for post in posts]

    # Thread title page
    title_page = HtmlSection()
    title_page.body.extend(
        BeautifulSoup('<h2 class="title">%s</h2>' % title, "html.parser")
    )
    title_page.body.extend(
        BeautifulSoup(
            '<h3 class="authors">%s</h2>' % ", ".join(sorted(authors)),
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
    resp = await auth_get(session, thread.url, params={"view": "flat"})
    soup = BeautifulSoup(await resp.text(), "html.parser")
    resp.close()
    thread.add_soup(soup)


async def download_chapters(
    slow_session: aiohttp.ClientSession,
    limiter: aiolimiter.AsyncLimiter,
    fast_session: aiohttp.ClientSession,
    threads: list[Thread],
    image_map: ImageMap,
    split: str,
):
    print("Downloading chapter texts")
    await tqdm.gather(
        *[
            download_chapter(slow_session, limiter, thread)
            for thread in threads
            if thread.compiled_sections is None
        ]
    )
    for thread in threads:
        if thread.compiled_sections is not None:
            for section in thread.compiled_sections:
                soup = BeautifulSoup(section.content, "html.parser")
                images = soup.find_all("img")
                for image in images:
                    image_map.add_cached_image_usage(get_attr(image, "src"))
        else:
            assert thread.soup is not None
            posts = thread.soup.find_all("div", class_="post-container")
            populate_image_map(posts, image_map)
    print("Downloading images")
    await tqdm.gather(
        *[
            download_image(fast_session, url, mapped_image)
            for (url, mapped_image) in image_map.map.items()
        ]
    )
    for thread in threads:
        if thread.compiled_sections is not None:
            continue
        assert thread.soup is not None
        # Evil posts (presumably caused by html copy-pasting?) may have post-containers within post-containers.
        # So we only check direct descendents of flat-post-replies.
        first_post = thread.soup.find("div", class_="post-post")
        assert first_post is not None
        replies_container = thread.soup.find("div", class_="flat-post-replies")
        assert replies_container is not None
        replies = replies_container.find_all(
            "div", class_="post-container", recursive=False
        )
        posts = chain([first_post], replies)
        thread.add_rendered_sections(
            list(render_posts(posts, image_map, thread.authors, thread.title, split))
        )


def map_permalinks_to_filenames(threads: list[Thread]) -> dict[str, str]:
    anchor_sections = {}
    for thread in threads:
        if thread.compiled_sections is not None:
            for (j, compiled_section) in enumerate(thread.compiled_sections):
                file_name = thread.section_name(j)
                soup = BeautifulSoup(compiled_section.content, "html.parser")
                for anchor in soup.find_all("a"):
                    match anchor.get("id"):
                        case None:
                            continue
                        case list():
                            raise ValueError("Multiple IDs for link")
                        case id:
                            match = COMPILED_REPLY_RE.match(id)
                    if match is None:
                        continue
                    reply_id = int(match.group(1))
                    permalink = f"/replies/{reply_id}#reply-{reply_id}"
                    anchor_sections[permalink] = file_name
        else:
            assert thread.rendered_sections is not None
            for (j, compiled_section) in enumerate(thread.rendered_sections):
                file_name = thread.section_name(j)
                for permalink in compiled_section.link_targets:
                    anchor_sections[permalink] = file_name
    return anchor_sections


def replace_or_tag_external_links_from_sections(threads: list[Thread]):
    anchor_sections = map_permalinks_to_filenames(threads)

    for thread in threads:
        if thread.compiled_sections is not None:
            # TODO: there are some unhandled edge cases here. If a link in a
            # cached thread goes from being external to internal or vice versa,
            # we'll have a problem. But it's hard to imagine why that would
            # happen.
            pass
        else:
            assert thread.rendered_sections is not None
            for section in thread.rendered_sections:
                for a in section.html.find_all("a"):
                    if "href" not in a.attrs:
                        continue
                    raw_url = get_attr(a, "href")
                    url = urlparse(raw_url)
                    if RELATIVE_REPLY_RE.match(raw_url) and raw_url in anchor_sections:
                        a["href"] = url._replace(path=anchor_sections[raw_url]).geturl()
                    else:
                        abs = ABSOLUTE_REPLY_RE.match(raw_url)
                        if abs is not None and abs.group("relative") in anchor_sections:
                            a["href"] = anchor_sections[abs.group("relative")]
                        else:  # External link
                            a["class"] = AttributeValueList(
                                a.get_attribute_list("class") + ["extlink"]
                            )
                            if url.netloc == "":
                                a["href"] = url._replace(
                                    scheme="https", netloc="glowfic.com"
                                ).geturl()


def compile_sections(threads: list[Thread]):
    for thread in threads:
        if thread.compiled_sections is not None:
            continue
        assert thread.rendered_sections is not None
        compiled_sections = []
        for j, section in enumerate(thread.rendered_sections):
            file_name = "Text/" + thread.section_name(j)
            compiled_section = EpubHtml(
                title=thread.title,
                file_name=file_name,
                media_type="application/xhtml+xml",
            )
            compiled_section.add_meta(name="glowfic-post-id", content=str(thread.id))
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


def compile_chapters(threads: list[Thread]):
    replace_or_tag_external_links_from_sections(threads)
    compile_sections(threads)


def generate_section_title_pages(sections: list[Section]):
    section_digits = len(str(len(sections)))
    for i, section in enumerate(sections):
        if section.title is None:
            # This the "null section". Normally for continuities with no
            # subsections, but can also exist as a "default" section in a
            # category with sections.
            # We don't generate title pages here.
            continue
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
            "section%.*i.xhtml" % (section_digits, i + 1)
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
) -> tuple[list[EpubHtml], list[str | EpubHtml]]:
    spine: list[str | EpubHtml] = ["nav"]
    match book_structure:
        case Thread():
            assert book_structure.compiled_sections is not None
            toc = [book_structure.compiled_sections[0]]
            spine += book_structure.compiled_sections
        case Section():
            toc = []
            spine = []
            for thread in book_structure.threads:
                assert thread.compiled_sections is not None
                toc.append(thread.compiled_sections[0])
                spine += thread.compiled_sections
        case Continuity():
            toc = []
            for section in book_structure.sections:
                assert section.title_page is not None
                toc.append(section.title_page)
                spine.append(section.title_page)
                for thread in section.threads:
                    assert thread.compiled_sections is not None
                    toc.append(thread.compiled_sections[0])
                    spine += thread.compiled_sections
            if book_structure.sectionless_threads is not None:
                for thread in book_structure.sectionless_threads.threads:
                    assert thread.compiled_sections is not None
                    toc.append(thread.compiled_sections[0])
                    spine += thread.compiled_sections
    return toc, spine


@dataclass(frozen=True)
class SectionInfo:
    id: int
    name: str
    order: int


async def get_book_structure(
    session: aiohttp.ClientSession, limiter: aiolimiter.AsyncLimiter, url: str
) -> Thread | Section | Continuity:

    if "posts" in url:
        target_url = "https://glowfic.com/api/v1%s" % urlparse(url).path
        await limiter.acquire()
        resp = await auth_get(session, target_url)
        post_json = await resp.json()
        return Thread(post_json)
    elif "board_sections" in url:
        await login(session, optional=True)
        section_id = int(urlparse(url).path.split("/")[-1])
        target_url = "https://glowfic.com/api/v1/subcontinuities/%d" % section_id
        await limiter.acquire()
        resp = await auth_get(session, target_url)
        section_json = await resp.json()
        board_id = section_json["board_id"]
        continuity = await get_continuity(session, limiter, board_id)
        for section in continuity.sections:
            if section.id == section_id:
                return section
        raise Exception("Unable to find section in board")
    elif "boards" in url:
        await login(session, optional=True)
        board_id = int(urlparse(url).path.split("/")[-1])
        return await get_continuity(session, limiter, board_id)
    else:
        raise ValueError(
            "URL contains neither 'posts' nor 'board_sections' nor 'boards'."
        )


async def get_continuity(
    session: aiohttp.ClientSession, limiter: aiolimiter.AsyncLimiter, board_id: int
):
    target_url = "https://glowfic.com/api/v1/boards/%d" % board_id
    await limiter.acquire()
    resp = await auth_get(session, target_url)
    board_json = await resp.json()
    title = board_json["name"]
    target_url = "https://glowfic.com/api/v1/boards/%d/posts" % board_id
    by_section: dict[SectionInfo | None, list[Any]] = {}
    for page in itertools.count(start=1):
        await limiter.acquire()
        resp = await auth_get(session, target_url, params={"page": page})
        posts_json = await resp.json()
        for post_json in posts_json["results"]:
            section = post_json.get("section")
            if section is not None:
                section = SectionInfo(
                    id=section["id"], name=section["name"], order=section["order"]
                )
            else:
                section = None  # Help the typechecker out
            if section not in by_section:
                by_section[section] = []
            by_section[section].append(post_json)
        if len(posts_json["results"]) < 25:
            # 25 results per page, so we're done now.
            break
    by_section_list = list((k, v) for (k, v) in by_section.items() if k is not None)
    by_section_list.sort(key=lambda kv: kv[0].order)
    sections = [Section.from_jsons(k.id, k.name, v) for (k, v) in by_section_list]
    if None in by_section:
        null_section = Section.from_jsons(None, None, by_section[None])
        return Continuity(board_id, title, sections, null_section)
    else:
        return Continuity(board_id, title, sections)


def get_images_as_epub_items(image_map: ImageMap) -> list[EpubItem]:
    items = []
    for mapped_image in image_map.map.values():
        if not isinstance(mapped_image.data, Succeeded):
            continue
        filename = mapped_image.get_filename()
        assert filename is not None
        items.append(
            EpubItem(
                uid=filename,
                file_name=filename,
                media_type=mapped_image.data.media_type,
                content=mapped_image.data.file,
            )
        )
    for hash, mapped_image in image_map.cached_images.items():
        if hash not in image_map.cached_posts_images:
            continue
        assert isinstance(mapped_image.data, Succeeded)
        filename = mapped_image.get_filename()
        assert filename is not None
        items.append(
            EpubItem(
                uid=filename,
                file_name=filename,
                media_type=mapped_image.data.media_type,
                content=mapped_image.data.file,
            )
        )
    return items
