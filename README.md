# Glowfic Downloader

Downloads glowfic! Invoke it like so:
```
./glowfic-dl.py https://glowfic.com/posts/5111 # download one post
./glowfic-dl.py https://glowfic.com/board_sections/703 # download a board section
./glowfic-dl.py https://glowfic.com/boards/215 # download a whole continuity
```
If you get errors, make sure you've got the dependencies installed:
```
pip3 install aiohttp aiolimiter bs4 ebooklib lxml tqdm tzdata
```
...or use a pipenv virtualenv for an extra guarantee of a clean and functional install:
```
pipenv install
pipenv shell
```

If you want to access private posts, you can put your `_glowfic_constellation_production` cookie into a file called `cookie`.

To run tests (requires `pytest` to be installed, or the pipenv shell to be active):
```
python3 -m pytest
```

I've tested it on Kindle and Kobo.
For Kindle, I recommend using Calibre to convert it to AZW3 and not MOBI: this gives significantly better formatting.
For Kobo, the formatting isn't great yet: it doesn't handle suggested page breaks particularly well.
Kobos also have trouble with very long chapters: since each post is 1 chapter, and many posts are very long, this can be an issue.

PRs welcome. I'd particularly welcome improvements to the Kobo results.
