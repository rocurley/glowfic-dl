from datetime import datetime, timezone
from itertools import chain
from typing import Optional
from typing_extensions import Self

from bs4 import BeautifulSoup
from ebooklib.epub import EpubHtml, EpubBook

from .helpers import make_filename_valid_for_epub3

###################
##   Templates   ##
###################


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
        ).replace(tzinfo=timezone.utc)
        self.description: str = post_json.get("description")
        self.authors = [author["username"] for author in post_json["authors"]]

        # Filled in by download_threads
        self.soup: BeautifulSoup | None = None
        # Filled in by render_threads
        self.rendered_sections: list[HtmlSection] | None = None
        # Filled in by compile_threads or by load_compiled_sections_from_old_book
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
            return datetime.fromisoformat(content.strip("Z")).replace(
                tzinfo=timezone.utc
            )
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
