from typing import Optional

from src.helpers import make_filename_valid_for_epub3


###############
##   Tests   ##
###############


class TestFilenameValidation:
    def run(self, in_filename: str, expected_out_filename: Optional[str] = None, should_error: bool = False):
        try:
            validated_filename = make_filename_valid_for_epub3(in_filename)
        except ValueError as e:
            if should_error:
                return
            else:
                raise e
        
        assert validated_filename == expected_out_filename

    # Filenames which should emerge unscathed

    def test_chapter_filename(self):
        filename = "chapter_01.xhtml"
        self.run(filename, filename)

    def test_megacontinuity_chinese_filename(self):
        filename = "1-0 (星期日没有日).xhtml"
        self.run(filename, filename)

    def test_kyubey_filename(self):
        filename = "／人◕ ‿‿ ◕人＼.css"
        self.run(filename, filename)

    def test_short_zalgo_filename(self):
        filename = "t̴h̵i̴s̸_̵i̴s̸_̷t̴e̵c̶h̵n̵i̸c̶a̵l̴l̵y̸_̶a̷l̸l̴o̷w̸e̸d̷.ncx"
        self.run(filename, filename)

    # Filenames which should emerge changed

    def test_aaaaa_filename(self):
        in_filename = ("A" * 300) + ".xhtml"
        out_filename = ("A" * 249) + ".xhtml"
        self.run(in_filename, out_filename)

    def test_filename_character_filtration(self):
        in_filename = "c/h\\a\"p*t:e<r\u007f_\u00120\u00802\ue001.\ufdefx\ufff8h\U000e0ffet\U000f8fffm\U00100000l"
        out_filename = "chapter_02.xhtml"
        self.run(in_filename, out_filename)

    def test_filename_ending_in_period(self):
        in_filename = "don't name your file like this.png."
        out_filename = "don't name your file like this.png"
        self.run(in_filename, out_filename)

    # Filenames which should raise an error

    def test_just_periods_filename(self):
        filename = "..."
        self.run(filename, should_error=True)

    def test_long_extension_filename(self):
        filename = "chapter_03." + ("B" * 300)
        self.run(filename, should_error=True)
