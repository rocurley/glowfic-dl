from bs4 import BeautifulSoup
import requests
from ebooklib import epub
from tqdm.asyncio import tqdm
import sys
from urllib.parse import urljoin, urlparse
from collections import OrderedDict
import asyncio
import aiohttp

# TODO:
# * Split large chapers somehow
# * Download posts, continuities &c explicitly, rather than automatically
#   advancing via next post
# * Strip out requests
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


def render_posts(posts, image_map, authors):
    out = BeautifulSoup(output_template, "html.parser")
    body = out.find("div")
    for post in posts:
        (rendered, author) = render_post(post, image_map)
        body.append(rendered)
        authors[author] = True
    return out


def download_chapter(url, image_map, authors):
    resp = requests.get(url, params={"view": "flat"})
    soup = BeautifulSoup(resp.text, "html.parser")
    resp.close()
    posts = soup.find_all("div", "post-container")
    title = soup.find("span", id="post-title").text.strip()
    posts_html = render_posts(posts, image_map, authors)
    print('Downloaded text of "%s"' % title)
    return (title, str(posts_html))


def find_all_urls(url):
    urls = []
    while True:
        urls.append(url)
        resp = requests.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        resp.close()
        next_buttons = [
            div
            for div in soup.find_all("div", "view-button")
            if div.text == "Next Post »"
        ]
        if len(next_buttons) == 0:
            return urls
        next_button = next_buttons[0]
        url = urljoin(url, next_button.parent["href"])


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


async def download_images(image_map):
    async with aiohttp.ClientSession() as session:
        in_flight = []
        for (k, v) in image_map.map.items():
            in_flight.append(download_image(session, k, v))
        return [image for image in await tqdm.gather(*in_flight) if image is not None]


def main():
    url = sys.argv[1]
    urls = find_all_urls(url)
    print("Found %i chapters" % len(urls))

    book = epub.EpubBook()
    image_map = ImageMap()
    authors = OrderedDict()

    chapters = []
    for (i, url) in enumerate(urls):
        (title, chapter_content) = download_chapter(url, image_map, authors)
        if i == 0:
            book.set_title(title)
            book_title = title
        chapter = epub.EpubHtml(title=title, file_name="chapter%i.html" % i)
        chapter.content = chapter_content
        chapter.add_link(href="style.css", rel="stylesheet", type="text/css")
        book.add_item(chapter)
        chapters.append(chapter)

    style = epub.EpubItem(
        uid="style", file_name="style.css", media_type="text/css", content=stylesheet
    )
    book.add_item(style)

    print("Downloading images")
    images = asyncio.run(download_images(image_map))

    for image in images:
        book.add_item(image)

    for author in authors.keys():
        book.add_author(author)

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    book.spine = ["nav"] + chapters
    out_path = "%s.epub" % book_title
    print("Saving book to %s" % out_path)
    epub.write_epub(out_path, book, {})


main()
