import pytest

from pdfacil.pipeline.fonts import (
    FontSpec,
    font_report,
    parse_postscript_name,
    split_camel,
    strip_subset_prefix,
)


def test_strip_subset_prefix():
    assert strip_subset_prefix("ABCDEF+Montserrat-Bold") == "Montserrat-Bold"
    assert strip_subset_prefix("Montserrat-Bold") == "Montserrat-Bold"
    assert strip_subset_prefix("ABC+Font") == "ABC+Font"


@pytest.mark.parametrize(
    "name, spec",
    [
        ("Montserrat-Bold", FontSpec("Montserrat", 700, False)),
        ("ABCDEF+OpenSans-SemiBoldItalic", FontSpec("Open Sans", 600, True)),
        ("PlayfairDisplay-Regular", FontSpec("Playfair Display", 400, False)),
        ("Montserrat Bold", FontSpec("Montserrat", 700, False)),
        ("Open Sans Regular", FontSpec("Open Sans", 400, False)),
        ("Arial,Bold", FontSpec("Arial", 700, False)),
        ("Poppins", FontSpec("Poppins", 400, False)),
        ("Lato-Black_2", FontSpec("Lato", 900, False)),
    ],
)
def test_parse_postscript_name(name, spec):
    assert parse_postscript_name(name) == spec


def test_split_camel():
    assert split_camel("PTSans") == "PT Sans"
    assert split_camel("DMSerifDisplay") == "DM Serif Display"


def test_registry_fetches_google_font_and_aliases(registry):
    font = registry.resolve("ABCDEF+Montserrat Bold")
    assert font is not None
    assert font.family == "Montserrat" and font.weight == 700
    # The real PostScript name is registered too, pointing at the same file.
    real = registry.lookup("Montserrat-Bold")
    assert real is not None and real.object_key == font.object_key
    assert registry.load(font).has_glyph(ord("A"))


def test_licensed_fonts_are_never_downloaded(google):
    # The CSS API answers "Helvetica" with licensed Helvetica LT Pro; the catalog check stops it.
    assert google.fetch(FontSpec("Helvetica")) is None
    assert google.fetch(FontSpec("Arimo", 900)) is None  # style the family does not have
    assert google.fetch(FontSpec("Arimo", 700)) is not None


@pytest.mark.parametrize(
    "name, family, weight, italic",
    [
        ("Helvetica-Bold", "Arimo", 700, False),
        ("Helvetica-Oblique", "Arimo", 400, True),
        ("ArialMT", "Arimo", 400, False),
        ("Times-Roman", "Tinos", 400, False),
        ("TimesNewRomanPS-BoldMT", "Tinos", 700, False),
        ("Courier", "Cousine", 400, False),
    ],
)
def test_standard_fonts_map_to_metric_compatible(registry, name, family, weight, italic):
    font = registry.resolve(name)
    assert (font.family, font.weight, font.italic, font.source) == (family, weight, italic, "metric")


def test_google_brand_families_are_allowed(registry):
    # Noto and Roboto are flagged as Google "brand" fonts in the catalog but are OFL-licensed.
    assert registry.resolve("NotoSans-Bold") is not None


def test_condensed_name_maps_to_width_axis(registry):
    condensed = registry.resolve("OpenSansCondensed-Bold")
    normal = registry.resolve("OpenSans-Bold")
    assert condensed is not None and normal is not None
    assert condensed.object_key != normal.object_key
    ratio = registry.load(condensed).text_length("MMMM", 10) / registry.load(normal).text_length("MMMM", 10)
    assert ratio < 0.9
    # The width instance must not take over the normal-width name.
    assert registry.lookup("OpenSans-Bold").object_key == normal.object_key


def test_registry_unknown_font(registry):
    assert registry.resolve("Zqxvern-Regular") is None


def test_user_font_wins_over_shared(registry, font_files):
    registry.resolve("Montserrat-Bold")
    data = font_files["OpenSans-Regular"].read_bytes()
    mine = registry.register_file(data, owner="u1")
    registry.add_alias("Montserrat-Bold", mine, owner="u1")
    assert registry.lookup("Montserrat-Bold", owner="u1").owner == "u1"
    assert registry.lookup("Montserrat-Bold").owner is None


def test_font_report(one_pager, registry):
    import pymupdf

    doc = pymupdf.open(stream=one_pager, filetype="pdf")
    report = font_report(doc, registry)
    assert set(report) == {"Montserrat Bold", "Montserrat Regular", "Open Sans Regular"}
    assert all(f.status == "mapped" for f in report.values())
    assert report["Open Sans Regular"].pages == [0]
