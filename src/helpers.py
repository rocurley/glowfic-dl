################
##   Consts   ##
################


FILENAME_BANNED_CHARS = '/\\"*:<>?|\u007f'
FILENAME_BANNED_CHAR_RANGES = (
    (ord("\u0000"), ord("\u001f")),  # C0
    (ord("\u0080"), ord("\u009f")),  # C1
    (ord("\ue000"), ord("\uf8ff")),  # Private Use Area
    (ord("\ufdd0"), ord("\ufdef")),  # Non-Characters in Arabic Presentation Forms-A
    (ord("\ufff0"), ord("\uffff")),  # Specials
    (ord("\U000e0000"), ord("\U000e0fff")),  # Tags and Variation Selectors Supplement
    (ord("\U000f0000"), ord("\U000fffff")),  # Supplementary Private Use Area-A
    (ord("\U00100000"), ord("\U0010ffff")),  # Supplementary Private Use Area-B
)


###################
##   Functions   ##
###################


def make_filename_valid_for_epub3(filename: str) -> str:
    filtered_filename = ""

    # Ensure filename contains only allowed chars
    for char in filename:
        if char in FILENAME_BANNED_CHARS:
            continue

        char_allowed = True
        for range_bottom, range_top in FILENAME_BANNED_CHAR_RANGES:
            char_ord = ord(char)
            if char_ord >= range_bottom and char_ord <= range_top:
                char_allowed = False
                break

        if char_allowed:
            filtered_filename += char

    # Ensure filename doesn't end in '.'
    while len(filtered_filename) > 0 and filtered_filename[-1] == ".":
        filtered_filename = filtered_filename[:-1]

    if len(filtered_filename) == 0:
        raise ValueError(
            "Attempted to put file into EPUB with filename containing only invalid characters and/or periods."
        )

    # Ensure filename is of allowed length, then return
    if len(filtered_filename.encode("utf-8")) <= 255:
        return filtered_filename
    else:
        # Assumptions:
        # * File extension exists and is <254 bytes in length
        # * Filenames have unique numerical identifiers before the 255-byte
        #   mark, such that truncation won't produce name collisions

        split_filename = filtered_filename.split(".")
        ext = split_filename[-1]
        ext_bytes = len(ext.encode("utf-8"))
        if ext_bytes > 254:
            raise ValueError(
                "Attempted to put file into EPUB with extension longer than 254 bytes."
            )

        name_truncated = ".".join(split_filename[:-1])[:-1]
        full_name_truncated = "%s.%s" % (name_truncated, ext)
        while len(full_name_truncated.encode("utf-8")) > 255:
            name_truncated = name_truncated[:-1]
            full_name_truncated = "%s.%s" % (name_truncated, ext)
        return full_name_truncated
