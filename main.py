from bs4 import BeautifulSoup
from ebooklib import epub
from tqdm.asyncio import tqdm
import sys
from urllib.parse import urljoin, urlparse
from collections import OrderedDict
import asyncio
import aiohttp
import aiolimiter
import os
import re

# TODO:
# * Better kobo handling
# * Rewrite internal links
#   Include linkbacks at the end of the thing that was linked to, eg: "This post was linked to from reply #114 of Mad investor chaos". <a>Return there</a>.
# * Less bad covers


class ImageMap:
    def __init__(self):
        self.map = {}
        self.next = 0

    def insert(self, url):
        if url not in self.map:
            path = urlparse(url).path
            ext = path.split(".")[-1]
            self.map[url] = "img%i.%s" % (self.next, ext)
            self.next += 1
        return self.map[url]


def render_post(post, image_map):
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
    post_div.extend([post.a])  # for linking to this post

    image = post.find("img", "icon")
    if image:
        local_image = BeautifulSoup('<img class="icon"></img>', "html.parser")
        local_image.find("img")["src"] = image_map.insert(image["src"])
        post_div.extend([header, local_image] + content.contents)
    else:
        post_div.extend([header] + content.contents)
    return (post_html, author)


stylesheet = """
img.icon {
    width:100px;
    float:left;
    margin-right: 1em;
    margin-bottom: 1em;
}
div.post {
    overflow: hidden;
    margin: 0.5em;
    background: white;
    page-break-inside:avoid;
}
div.posts {
    background: grey;
}
"""

output_template = """
<html>
<head>
</head>
<body>
<div class="posts">
</div>
</body>
</html>
"""

SECTION_SIZE_LIMIT = 200000


def render_posts(posts, image_map, authors):
    out = BeautifulSoup(output_template, "html.parser")
    body = out.find("div")
    size = 0
    for post in posts:
        (rendered, author) = render_post(post, image_map)
        post_size = len(rendered.encode())
        if size + post_size > SECTION_SIZE_LIMIT and size > 0:
            yield out
            out = BeautifulSoup(output_template, "html.parser")
            body = out.find("div")
            size = 0
        size += post_size
        body.append(rendered)
        authors[author] = True
    yield out


async def download_chapter(session, limiter, i, url, image_map, authors):
    await limiter.acquire()
    resp = await session.get(url, params={"view": "flat"})
    soup = BeautifulSoup(await resp.text(), "html.parser")
    resp.close()
    posts = soup.find_all("div", "post-container")
    title = validate_tag(soup.find("span", id="post-title"), soup).text.strip()
    return (title, list(render_posts(posts, image_map, authors)))
    """
    sections = []
    for (j, section_html) in enumerate(render_posts(posts, image_map, authors)):
        file_name = "chapter%i_%i.html" % (i, j)
        for a in section_html.find_all("div", "post").a:
            anchor_sections[a.id] = file_name
        section = epub.EpubHtml(title=title, file_name=file_name)
        section.content = str(section_html)
        section.add_link(href="style.css", rel="stylesheet", type="text/css")
        sections.append(section)
    return sections
    """


REPLY_RE = re.compile(r"/replies/\d*")


def compile_chapters(chapters):
    anchor_sections = {}
    for (i, (title, sections)) in enumerate(chapters):
        for (j, section_html) in enumerate(sections):
            file_name = "chapter%i_%i.html" % (i, j)
            for post in section_html.find_all("div", "post"):
                print(post)
                anchor_sections[post.a["id"]] = file_name
    for (i, (title, sections)) in enumerate(chapters):
        for (j, section_html) in enumerate(sections):
            for a in section_html.find_all("a"):
                if "href" not in a.attrs:
                    continue
                url = urlparse(a["href"])
                # TODO: links to posts
                if not REPLY_RE.match(url.path):
                    print("Skipping", url.path)
                    continue
                if url.fragment in anchor_sections:
                    a["href"] = url._replace(
                        path=anchor_sections[url.fragment]
                    ).geturl()
                else:
                    print("Skipping", url.path)
    for (i, (title, sections)) in enumerate(chapters):
        compiled_sections = []
        for (j, section_html) in enumerate(sections):
            file_name = "chapter%i_%i.html" % (i, j)
            section = epub.EpubHtml(title=title, file_name=file_name)
            section.content = str(section_html)
            section.add_link(href="style.css", rel="stylesheet", type="text/css")
            compiled_sections.append(section)
        yield compiled_sections


def validate_tag(tag, soup):
    if tag is not None:
        return tag
    err = soup.find("div", "flash error")
    if err is not None:
        raise RuntimeError(err.text.strip())
    else:
        raise RuntimeError("Unknown error: tag missing")


GLOWFIC_ROOT = "https://glowfic.com"


async def get_post_urls_and_title(session, limiter, url):
    if "posts" in url:
        return (None, [url])
    if "board_sections" in url or "boards" in url:
        await limiter.acquire()
        resp = await session.get(url)
        soup = BeautifulSoup(await resp.text(), "html.parser")
        rows = validate_tag(soup.find("div", id="content"), soup).find_all(
            "td", "post-subject"
        )
        posts = [urljoin(GLOWFIC_ROOT, row.find("a")["href"]) for row in rows]
        title = soup.find("th", "table-title").text.strip()
        return (title, posts)


async def download_image(session, url, id):
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


async def download_images(session, image_map):
    in_flight = []
    for (k, v) in image_map.map.items():
        in_flight.append(download_image(session, k, v))
    return [image for image in await tqdm.gather(*in_flight) if image is not None]


COOKIE_NAME = "_glowfic_constellation_production"


async def main():
    cookies = {}
    if os.path.exists("cookie"):
        with open("cookie") as fin:
            raw = fin.read()
            (name, cookie) = raw.split("=")
            if name != COOKIE_NAME:
                raise ValueError(
                    'cookie file must start with "%s=" (no quotes)' % COOKIE_NAME
                )
            cookies[COOKIE_NAME] = cookie.strip()
    slow_conn = aiohttp.TCPConnector(limit_per_host=1)
    print(cookies)
    async with aiohttp.ClientSession(
        connector=slow_conn, cookies=cookies
    ) as slow_session:
        async with aiohttp.ClientSession() as fast_session:
            limiter = aiolimiter.AsyncLimiter(1, 1)
            url = sys.argv[1]
            (book_title, urls) = await get_post_urls_and_title(
                slow_session, limiter, url
            )
            print("Found %i chapters" % len(urls))

            book = epub.EpubBook()
            image_map = ImageMap()
            authors = OrderedDict()

            print("Downloading chapter texts")
            chapters = await tqdm.gather(
                *[
                    download_chapter(slow_session, limiter, i, url, image_map, authors)
                    for (i, url) in enumerate(urls)
                ]
            )
            chapters = list(compile_chapters(chapters))
            for chapter in chapters:
                for section in chapter:
                    book.add_item(section)
            if book_title is None:
                book_title = chapters[0][0].title
            book.set_title(book_title)

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
                section for section in chapter for chapter in chapters
            ]
            out_path = "%s.epub" % book_title
            print("Saving book to %s" % out_path)
            epub.write_epub(out_path, book, {})


asyncio.run(main())
