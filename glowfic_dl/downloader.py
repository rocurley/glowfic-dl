import asyncio
from itertools import chain
import itertools
from typing import Any
from urllib.parse import urlparse

import aiohttp
import aiolimiter
from dataclasses import dataclass
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm

from .helpers import get_attr
from .auth import auth_get, login
from .render import (
    Continuity,
    ImageMap,
    MappedImage,
    Section,
    Succeeded,
    Thread,
    populate_image_map,
    render_posts,
)


class Downloader:
    def __init__(self):
        self.limiter = aiolimiter.AsyncLimiter(1, 1)
        self.slow_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit_per_host=1)
        )
        self.fast_session = aiohttp.ClientSession()

    async def get_book_structure(self, url: str) -> Thread | Section | Continuity:

        if "posts" in url:
            target_url = "https://glowfic.com/api/v1%s" % urlparse(url).path
            await self.limiter.acquire()
            resp = await auth_get(self.slow_session, target_url)
            post_json = await resp.json()
            return Thread(post_json)
        elif "board_sections" in url:
            await login(self.slow_session, optional=True)
            section_id = int(urlparse(url).path.split("/")[-1])
            target_url = "https://glowfic.com/api/v1/subcontinuities/%d" % section_id
            await self.limiter.acquire()
            resp = await auth_get(self.slow_session, target_url)
            section_json = await resp.json()
            board_id = section_json["board_id"]
            continuity = await self.get_continuity(board_id)
            for section in continuity.sections:
                if section.id == section_id:
                    return section
            raise Exception("Unable to find section in board")
        elif "boards" in url:
            await login(self.slow_session, optional=True)
            board_id = int(urlparse(url).path.split("/")[-1])
            return await self.get_continuity(board_id)
        else:
            raise ValueError(
                "URL contains neither 'posts' nor 'board_sections' nor 'boards'."
            )

    async def get_continuity(self, board_id: int):
        @dataclass(frozen=True)
        class SectionInfo:
            id: int
            name: str
            order: int

        target_url = "https://glowfic.com/api/v1/boards/%d" % board_id
        await self.limiter.acquire()
        resp = await auth_get(self.slow_session, target_url)
        board_json = await resp.json()
        title = board_json["name"]
        target_url = "https://glowfic.com/api/v1/boards/%d/posts" % board_id
        by_section: dict[SectionInfo | None, list[Any]] = {}
        for page in itertools.count(start=1):
            await self.limiter.acquire()
            resp = await auth_get(self.slow_session, target_url, params={"page": page})
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

    async def download_chapter(
        self,
        thread: Thread,
    ):
        await self.limiter.acquire()
        resp = await auth_get(self.slow_session, thread.url, params={"view": "flat"})
        soup = BeautifulSoup(await resp.text(), "html.parser")
        resp.close()
        thread.add_soup(soup)

    # TODO: Split out rendering stuff
    async def download_chapters(
        self,
        threads: list[Thread],
        image_map: ImageMap,
        split: str,
    ):
        print("Downloading chapter texts")
        await tqdm.gather(
            *[
                self.download_chapter(thread)
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
                self.download_image(url, mapped_image)
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
                list(
                    render_posts(posts, image_map, thread.authors, thread.title, split)
                )
            )

    async def download_image(self, url: str, mapped_image: MappedImage):
        if isinstance(mapped_image.data, Succeeded):
            return
        try:
            headers = {}
            if "imgur" in url:
                headers = {"user-agent": "curl/8.1.1", "accept": "*/*"}
            async with self.fast_session.get(url, timeout=15, headers=headers) as resp:
                file = await resp.read()
                if len(file) == 0:
                    print("Empty download for %s" % url)
                    file = None

        except (aiohttp.ClientError, asyncio.TimeoutError):
            print("Failed to download %s" % url)
            file = None
        mapped_image.add_file(file, url)
