# Glowfic Downloader

Downloads glowfic! Invoke it like so:
```
python3 main.py https://glowfic.com/posts/5111 # download one post
python3 main.py https://glowfic.com/board_sections/703 # download a board section
python3 main.py https://glowfic.com/boards/215 # download a whole continuity
```
If you get errors, make sure you've got the dependencies installed:
```
pip3 install ebooklib tqdm bs4 aiohttp aiolimiter
```

If you want to access private posts, you can put your `_glowfic_constellation_production` cookie into a file called `cookie`.

I've tested it on Kindle and Kobo.
For Kindle, I recommend using Calibre to convert it to AZW3 and not MOBI: this gives significantly better formatting.
For Kobo, the formatting isn't great yet: it doesn't handle suggested page breaks particularly well.
Kobos also have trouble with very long chapters: since each post is 1 chapter, and many posts are very long, this can be an issue.

PRs welcome. I'd particularly welcome improvements to the Kobo results.
