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

import streamlit as st
import streamlit.components.v1 as components
from stqdm import stqdm

# TODO:
# * Better kobo handling
# * Rewrite internal links
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

    image = post.find("img", "icon")
    if image:
        local_image = BeautifulSoup('<img class="icon"></img>', "html.parser")
        # Just hotlink to the image, instead of downloading it
        local_image.find("img")["src"] = image["src"]
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
    font-family: 'Lora', serif;
    font-size: 1.1em;
}
div.posts {
    background: white;
}
"""

output_template = f"""
<html>
<head>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lora:wght@400;700&display=swap" rel="stylesheet">
<style>{stylesheet}</style>
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


async def download_chapter(session, i, url, image_map, authors, container):
    # Apparently this function prints `None` on Streamlit?
    # await limiter.acquire()
    resp = await session.get(url, params={"view": "flat"})
    soup = BeautifulSoup(await resp.text(), "html.parser")
    resp.close()
    posts = soup.find_all("div", "post-container")
    title = validate_tag(soup.find("span", id="post-title"), soup).text.strip()
    sections = []
    for (j, section_html) in enumerate(render_posts(posts, image_map, authors)):
        filename = "chapter%i_%i.html" % (i, j)
        section = epub.EpubHtml(title=f"{title} {i+1}.{j+1}", file_name=filename)
        section.content = str(section_html)
        section.add_link(href="style.css", rel="stylesheet", type="text/css")
        sections.append(section)
        with container:
            with st.expander(section.title, expanded=(i == 0 and j == 0)):
                components.html(section.content, height=800, scrolling=True)
    return sections


def validate_tag(tag, soup):
    if tag is not None:
        return tag
    err = soup.find("div", "flash error")
    if err is not None:
        raise RuntimeError(err.text.strip())
    else:
        raise RuntimeError("Unknown error: tag missing")


GLOWFIC_ROOT = "https://glowfic.com"


async def get_post_urls_and_title(session, url):
    if "posts" in url:
        return (None, [url])
    if "board_sections" in url or "boards" in url:
        # await limiter.acquire()
        resp = await session.get(url)
        soup = BeautifulSoup(await resp.text(), "html.parser")
        rows = validate_tag(soup.find("div", id="content"), soup).find_all(
            "td", "post-subject"
        )
        posts = [urljoin(GLOWFIC_ROOT, row.find("a")["href"]) for row in rows]
        title = soup.find("th", "table-title").contents[0].strip()
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

st.set_page_config(page_title="Glowflow Reader", page_icon="🌟")
params = st.experimental_get_query_params()

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
    async with aiohttp.ClientSession(
        connector=slow_conn, cookies=cookies
    ) as slow_session:

        # Accept `?post=5111`; otherwise, default to Mad Investor Chaos
        post = params["post"][0] if "post" in params else 4582 
        default_url = f"https://glowfic.com/posts/{post}"
        st.sidebar.write("# Glowflow Reader")
        url = st.sidebar.text_input(label="Glowfic URL", value=default_url)

        st.sidebar.write("*Made by [Austin](https://manifold.markets/Austin), who stole [rocurley's code](https://github.com/rocurley/glowfic-dl)*")

        (book_title, urls) = await get_post_urls_and_title(slow_session, url)

        book = epub.EpubBook()
        image_map = ImageMap()
        authors = OrderedDict()

        container = st.container()
        with st.sidebar:
            _results = await stqdm.gather(
                *[
                    download_chapter(slow_session, i, url, image_map, authors, container)
                    for (i, url) in enumerate(urls)
                ]
            )


asyncio.run(main())