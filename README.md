# Glowfic Downloader

Downloads glowfic! Invoke it like so:
```
python3 main.py https://glowfic.com/posts/5111
```
If you get errors, make sure you've got the dependencies installed:
```
pip3 install requests ebooklib tqdm bs4
```
It produces an epub of the entire board section starting from the post you give it.
I've tested it on Kindle and Kobo.
For Kindle, I recommend using Calibre to convert it to AZW3 and not MOBI: this gives significantly better formatting.
For Kobo, the formatting isn't great yet: it doesn't handle suggested page breaks particularly well.
Kobos also have trouble with very long chapters: since each post is 1 chapter, and many posts are very long, this can be an issue.

PRs welcome. I'd particularly welcomn improvements to the Kobo results.
