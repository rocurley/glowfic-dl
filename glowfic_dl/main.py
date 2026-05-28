import argparse

from ebooklib import epub

from glowfic_dl.downloader import Downloader

from .helpers import make_filename_valid_for_epub3
from .render import (
    render_threads,
    stylesheet,
    Continuity,
    ImageMap,
    Section,
    Thread,
    compile_threads,
    generate_section_title_pages,
    generate_toc_and_spine,
    get_images_as_epub_items,
)

# TODO:
# * Better kobo handling
# * Rewrite internal links
#   Include linkbacks at the end of the thing that was linked to, eg: "This
#   post was linked to from reply #114 of Mad investor chaos". <a>Return
#   there</a>.
# * Less bad covers
# * Increase configurability of title page content


###################
##   Functions   ##
###################


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download glowfic from the Glowfic Constellation."
    )

    parser.add_argument("url", help="glowfic thread, section, or board URL")
    parser.add_argument(
        "-s",
        "--split",
        choices=["none", "if_large", "every_post"],
        default="if_large",
        help="how often (if at all) to split the output book's internal representations of threads into multiple files. 'none' means no splits occur except at thread boundaries; 'if_large' splits threads over 200kB in size after every 200kB; 'every_post' splits after each post irrespective of size. Default: if_large",
    )

    return parser.parse_args()


async def main():
    args = get_args()
    await download_ebook(args.url, args.split, "title")


# TODO: Probably want to add a step where we fetch the barebones metadata (so we can check the last update).
# Maybe want to make a new type for that metadata.
async def download_ebook(url: str, split: str, filename_mode: str) -> str:
    async with Downloader() as downloader:
        book_structure = await downloader.get_book_structure(url)
        match book_structure:
            case Thread():
                print("Found 1 thread")
            case Section():
                print("Found %i threads" % len(book_structure.threads))
            case Continuity():
                print(
                    "Found %i sections and %i threads"
                    % (len(book_structure.sections), len(book_structure.threads))
                )

        out_path = gen_ebook_path(filename_mode, book_structure)
        try:
            old_book = epub.read_epub(out_path)
        except:
            old_book = None
        image_map = ImageMap()
        if old_book:
            image_map.populate_from_book(old_book)
            book_structure.load_compiled_sections_from_old_book(old_book)

        await downloader.download_threads(
            book_structure.threads,
        )
        image_map.populate_from_threads(book_structure.threads)
        await downloader.download_images(image_map)
    render_threads(book_structure.threads, image_map, split)
    compile_threads(book_structure.threads)

    book = assemble_book(book_structure, image_map)

    print("Saving book to %s" % out_path)
    epub.write_epub(out_path, book, {})
    return out_path


def assemble_book(book_structure: Thread | Section | Continuity, image_map: ImageMap):
    book = epub.EpubBook()
    for thread in book_structure.threads:
        assert thread.compiled_sections is not None
        for section in thread.compiled_sections:
            book.add_item(section)
    if isinstance(book_structure, Continuity):
        generate_section_title_pages(book_structure.sections)
        for section in book_structure.sections:
            book.add_item(section.title_page)
    book.set_title(book_structure.title)

    style = epub.EpubItem(
        uid="style",
        file_name="style.css",
        media_type="text/css",
        content=stylesheet,
    )
    book.add_item(style)

    for image in get_images_as_epub_items(image_map):
        book.add_item(image)

    authors = set()
    for thread in book_structure.threads:
        for author in thread.authors:
            authors.add(author)
    for author in sorted(authors):
        book.add_author(author)

    book.toc, book.spine = generate_toc_and_spine(book_structure)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    return book


def gen_ebook_path(filename_mode: str, book_structure: Thread | Section | Continuity):
    match filename_mode:
        case "title":
            return make_filename_valid_for_epub3("%s.epub" % book_structure.title)
        case "url":
            match book_structure:
                case Thread():
                    return f"replies_{book_structure.id}.epub"
                case Section():
                    return f"board_sections_{book_structure.id}.epub"
                case Continuity():
                    return f"boards_{book_structure.id}.epub"
        case _:
            raise Exception(
                f"Unexpected filename mode: should be url or title but got {filename_mode}"
            )
