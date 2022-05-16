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
import pickle
import hashlib
import lxml
import cchardet
import re

import streamlit as st
import streamlit.components.v1 as components
from stqdm import stqdm

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
    header = BeautifulSoup("<p><strong></strong></p>", "lxml")
    header.find("strong").string = " / ".join(
        [x for x in [character, screen_name, author] if x is not None]
    )

    post_html = BeautifulSoup('<div class="post"></div>', "lxml")
    post_div = post_html.find("div")

    image = post.find("img", "icon")
    if image:
        local_image = BeautifulSoup('<img class="icon"></img>', "lxml")
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
"""

template = f"""
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

SECTION_SIZE_LIMIT = 100000


def render_posts(posts, image_map, authors):
    out = BeautifulSoup(template, "lxml")
    body = out.find("div")
    size = 0
    for post in posts:
        (rendered, author) = render_post(post, image_map)
        post_size = len(rendered.encode())
        if size + post_size > SECTION_SIZE_LIMIT and size > 0:
            yield out
            out = BeautifulSoup(template, "lxml")
            body = out.find("div")
            size = 0
        size += post_size
        body.append(rendered)
        authors[author] = True
    yield out

CACHE_DIR = "cache"
# Cache urls to disk using the sha256 hash of the url
def cache_set(key, value):
    hash = hashlib.sha3_256(key.encode()).hexdigest()
    filename = os.path.join(CACHE_DIR, f'{hash}.cache')
    pickle.dump(value, open(filename, "wb"))

def cache_get(key):
    st.session_state.cache_accessed.append(key)
    # console.log(f"Cache accessed: {key}")
    hash = hashlib.sha3_256(key.encode()).hexdigest()
    filename = os.path.join(CACHE_DIR, f'{hash}.cache')
    try:
        with open(filename, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None

def cache_clear(key):
    hash = hashlib.sha3_256(key.encode()).hexdigest()
    filename = os.path.join(CACHE_DIR, f'{hash}.cache')
    try:
        os.remove(filename)
    except FileNotFoundError:
        pass

def cache_clear_all():
    for key in st.session_state.cache_accessed:
        cache_clear(key)

async def get_html(session, url):
    html = cache_get(url)
    if not html:
        resp = await session.get(url, params={"view": "flat"})
        html = await resp.text()
        resp.close()
        cache_set(url, html)
    return html


async def download_chapter(session, i, url, image_map, authors):
    key = f"{url}|||{i}"
    sections = cache_get(key)
    if not sections:
        html = await get_html(session, url)
        # Parsing here seems to be the slowest part.
        soup = BeautifulSoup(html, "lxml")
        posts = soup.find_all("div", "post-container")
        title = validate_tag(soup.find("span", id="post-title"), soup).text.strip()
        sections = []
        for (j, section_html) in enumerate(render_posts(posts, image_map, authors)):
            sections.append([f"{title} {i+1}.{j+1}", str(section_html)])
        cache_set(key, sections)
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
        html = await get_html(session, url)
        soup = BeautifulSoup(html, "lxml")
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

# Inject the theme stylesheet right before `</head>` and return the new html
def addTheme(html):
    dark_mode = st.session_state.dark_mode
    text_color = "#31333F" if not dark_mode else "#FAFAFA"
    background_color = "#FFFFFF" if not dark_mode else "#0E1117"
    # A width of 0 = at most 600px wide
    width = st.session_state.width
    width = f"{width}px" if width else "100%"
    max_width = "100%" if st.session_state.width else "600px"

    theme = f"""
    <style>
    div.post {{
        color: {text_color};
        background: {background_color};
        font-size: {st.session_state.font_size}em;
        width: {width};
        max-width: {max_width};
        margin: 0 auto;
    }}
    div.posts {{
        background: {background_color};
    }}
    </style>
    """

    # Fast version: using regex instead of BeautifulSoup
    return re.sub(r"(</head>)", r"{}\n\1".format(theme), html, flags=re.MULTILINE)

st.set_page_config(page_title="Glowflow Reader", page_icon="🌟", layout="wide")
params = st.experimental_get_query_params()
try:
    noop = lambda *args, **kwargs: None
    noop(st.session_state.cache_accessed)
except AttributeError:
    st.session_state.cache_accessed = []

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

        with st.sidebar:
            st.write("# Glowflow Reader")
            url = st.text_input(label="Glowfic URL", value=default_url)

            with st.expander('Advanced'):
                st.checkbox("Dark mode", value=False, key="dark_mode")
                if st.session_state.dark_mode:
                    st.info("Try `Settings > Theme > Dark` too!")
                
                # Note: If width is set to None/0, then go full-width
                st.slider("Box height (px)", min_value=100, max_value=2000, value=800, step=40,key="height")
                st.slider("Text width (px)", min_value=0, max_value=2000, value=0, step=60, key="width")
                st.slider("Font size (rem)", min_value=0.5, max_value=2.0, value=1.1, step=0.1, key="font_size")
                if st.button("Clear cache"):
                    cache_clear_all()

                

            st.write("""
            ***
            *Made by [Austin](https://manifold.markets/Austin), 
            based on [rocurley's code](https://github.com/rocurley/glowfic-dl)*

            *Also: try [Glowfic to Epub](https://share.streamlit.io/akrolsmir/glowflow/main/epub.py)!*
            """)

        (book_title, urls) = await get_post_urls_and_title(slow_session, url)

        book = epub.EpubBook()
        image_map = ImageMap()
        authors = OrderedDict()

        chapters = []
        with st.sidebar:
            chapters = await stqdm.gather(
                *[
                    download_chapter(slow_session, i, url, image_map, authors)
                    for (i, url) in enumerate(urls)
                ]
            )
        for chapter in chapters:
            for (i, [title, html]) in enumerate(chapter):
                with st.expander(title, expanded= i == 0):
                    components.html(addTheme(html), width=None, height=st.session_state.height, scrolling=True)



asyncio.run(main())
