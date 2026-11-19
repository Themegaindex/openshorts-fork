"""Advance widths of the caption fonts (Windows faces and the Docker substitutes)."""
import pytest

import caption_fonts as cf

FAMILIES = ["Verdana", "Arial", "Arial Black", "Impact", "Helvetica", "Georgia", "Courier New"]


def test_every_listed_face_covers_the_whole_charset():
    for face, widths in cf._FACE_WIDTHS.items():
        assert len(widths) == len(cf._CHARSET), face
        assert all(0 < width < 1500 for width in widths), face


def test_widths_follow_the_libass_font_size():
    # Courier New is monospaced: 1229 of 2048 units, sized by 1705 + 615.
    assert cf.text_width("eigentlich", "Courier New", "win32") == pytest.approx(10 * 0.530)
    assert cf.text_width("", "Verdana", "win32") == 0
    # Verdana Bold sized by usWinAscent + usWinDescent = 2059 + 430 units:
    # its "n" is about 0.586 font sizes wide, its "W" about 0.928.
    assert cf.text_width("n", "Verdana", "win32") == pytest.approx(0.586, abs=0.002)
    assert cf.text_width("W", "Verdana", "win32") == pytest.approx(0.928, abs=0.002)


@pytest.mark.parametrize("family", FAMILIES)
def test_docker_estimate_covers_the_windows_face_too(family):
    """Outside Windows a family takes the wider of the Docker substitute and
    the original face, so it holds wherever either is installed."""
    for text in ("eigentlich", "GROSSBUCHSTABEN", "Überbrückt, 1234!"):
        assert cf.text_width(text, family, "linux") >= cf.text_width(text, family, "win32") - 1e-9


def test_docker_arial_black_is_the_wider_dejavu_sans():
    # docker/fonts.conf renders Arial Black as DejaVu Sans, which is wider per
    # font size than the real Arial Black.
    assert cf.text_width("WIRKLICH", "Arial Black", "linux") > cf.text_width("WIRKLICH", "Arial Black", "win32")


def test_family_names_are_matched_loosely():
    assert cf.text_width("Hallo", "  arial   BLACK ", "win32") == cf.text_width("Hallo", "Arial Black", "win32")


def test_unknown_families_and_characters_are_estimated_wide():
    for family in FAMILIES:
        assert cf.text_width("eigentlich", "Comic Sans MS", "win32") >= cf.text_width("eigentlich", family, "win32")
        assert cf.text_width("eigentlich", "Comic Sans MS", "linux") >= cf.text_width("eigentlich", family, "linux")
    assert cf.text_width("漢字", "Verdana", "win32") == 2 * cf.WIDE_CHARACTER_WIDTH


@pytest.mark.parametrize("accented,plain", [
    ("değişiklik", "degisiklik"),   # Turkish, listed letters
    ("właśnie", "wlasnie"),         # Polish, listed letters
    ("příliš", "prilis"),           # Czech, listed letters
    ("Hưởng", "Hương"),             # Vietnamese ở: decomposed down to the listed ơ
    ("Ǎǐǒ", "Aio"),                 # Latin Extended-B: decomposed to the base letter
])
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_accented_letters_are_as_wide_as_their_letters(accented, plain, platform):
    """Unlisted accented letters used to count as one full font size."""
    assert cf.text_width(accented, "Verdana", platform) == pytest.approx(
        cf.text_width(plain, "Verdana", platform), rel=0.03,
    )


def test_listed_letters_keep_their_own_width():
    # Latin Extended-A is listed exactly: Slovak ľ is wider than l, Polish ł
    # is not decomposable at all.
    assert cf.text_width("ľ", "Verdana", "win32") > cf.text_width("l", "Verdana", "win32")
    assert cf.text_width("ł", "Verdana", "win32") < 2 * cf.text_width("l", "Verdana", "win32")
    assert all(chr(code) in cf._CHARSET for code in range(0x100, 0x180))


def test_combining_marks_add_no_width():
    assert cf.text_width("ślow", "Verdana", "win32") == cf.text_width("slow", "Verdana", "win32")
    # A decomposed horn letter still counts the horn.
    assert cf.text_width("ư", "Verdana", "win32") == cf.text_width("u", "Verdana", "win32")
    assert cf.text_width("ư", "Verdana", "win32") >= cf.text_width("u", "Verdana", "win32")


def test_the_generator_is_not_part_of_the_app():
    import pathlib
    root = pathlib.Path(__file__).parents[1]
    for module in ("caption_fonts.py", "caption_placement.py", "subtitles.py", "app.py", "main.py"):
        assert "caption_font_widths" not in (root / module).read_text(encoding="utf-8").replace(
            "tools/caption_font_widths.py", ""), module


def test_greek_and_cyrillic_are_listed_with_real_widths():
    for word in ("программа", "сегодня", "πρόγραμμα", "Ґанок"):
        per_letter = cf.text_width(word, "Verdana", "win32") / len(word)
        assert 0.4 < per_letter < 0.75, word   # used to count 1.0 per letter
    assert all(char in cf._CHARSET for char in "абвгдежзийклмнопрстуфхцчшщъыьэюяёєіїАБВΑΒΓΔαβγδω")


@pytest.mark.parametrize("text,width", [
    ("Բարեւ", 5 * cf.UNKNOWN_LETTER_WIDTH),       # Armenian: a cautious letter width
    ("الذكاء", 6 * cf.UNKNOWN_LETTER_WIDTH),      # Arabic
    ("人工", 2 * cf.WIDE_CHARACTER_WIDTH),         # wide East Asian characters
    ("😀", cf.WIDE_CHARACTER_WIDTH),               # emoji
    ("​‍", 0.0),                        # format characters
])
def test_characters_outside_the_table(text, width):
    assert cf.text_width(text, "Verdana", "win32") == pytest.approx(width)
    assert cf.UNKNOWN_LETTER_WIDTH < cf.WIDE_CHARACTER_WIDTH


@pytest.mark.parametrize("word,splittable", [
    ("Donaudampfschifffahrt", True), ("программа", True), ("人工智能", True),
    ("الاصطناعي", False), ("बुद्धिमत्ता", False), ("ปัญญาประดิษฐ์", False), ("תוכנית", False),
])
def test_joined_and_right_to_left_scripts_are_never_split(word, splittable):
    assert cf.can_split(word) is splittable
