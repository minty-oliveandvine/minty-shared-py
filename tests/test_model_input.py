"""minty_shared.model_input: the small copy a model reads (owner, 2026-11-04)."""

import io

import pytest

PIL = pytest.importorskip("PIL")
pikepdf = pytest.importorskip("pikepdf")

from PIL import Image, JpegImagePlugin  # noqa: E402

from minty_shared import model_input  # noqa: E402


def _jpeg(width, height, colour=(200, 120, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _png_with_alpha() -> bytes:
    img = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    img.paste((10, 20, 30, 255), (0, 0, 150, 200))  # left half opaque, right half transparent
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pdf(pages: int) -> bytes:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


def test_a_large_photo_is_capped_on_its_longest_side_and_sent_as_jpeg():
    data, mime = model_input.prepare_for_model(_jpeg(4000, 3000), "image/jpeg")
    assert mime == "image/jpeg"
    assert _size(data) == (2048, 1536)


def test_a_jpeg_over_twice_the_cap_is_decoded_at_reduced_scale(monkeypatch):
    """The memory guard: a JPEG more than twice ``max_side`` is read through the draft path (1/2,
    1/4 or 1/8 scale straight from the file), so its full bitmap is never built - here 4000x3000
    against a 1000 cap decodes at 1000x750 (1/4). A landscape photo too: Pillow 11's thumbnail()
    asked draft() for a square and never reduced one. Pillow allocates in C, so the proof is the
    decode size, not tracemalloc."""
    decoded = []
    real_load = JpegImagePlugin.JpegImageFile.load

    def load(self):
        decoded.append(self.size)
        return real_load(self)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", load)
    data, _ = model_input.prepare_for_model(_jpeg(4000, 3000), "image/jpeg", max_side=1000)
    assert decoded and decoded[0] == (1000, 750)
    assert _size(data) == (1000, 750)


def test_a_48mp_photo_is_decoded_at_half_scale_for_the_default_cap(monkeypatch):
    decoded = []
    real_load = JpegImagePlugin.JpegImageFile.load
    monkeypatch.setattr(
        JpegImagePlugin.JpegImageFile,
        "load",
        lambda self: (decoded.append(self.size), real_load(self))[1],
    )
    data, _ = model_input.prepare_for_model(_jpeg(8000, 6000), "image/jpeg")
    assert decoded[0] == (4000, 3000)
    assert _size(data) == (2048, 1536)


def test_a_small_image_is_not_upscaled():
    data, _ = model_input.prepare_for_model(_jpeg(640, 480), "image/jpeg")
    assert _size(data) == (640, 480)


def test_max_side_is_tunable():
    data, _ = model_input.prepare_for_model(_jpeg(3000, 1000), "image/jpeg", max_side=1500)
    assert _size(data) == (1500, 500)


def test_a_png_with_transparency_becomes_a_white_backed_jpeg():
    data, mime = model_input.prepare_for_model(_png_with_alpha(), "image/png")
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as img:
        assert img.mode == "RGB"
        r, g, b = img.getpixel((280, 100))  # the transparent half
        assert min(r, g, b) > 240
        r, g, b = img.getpixel((20, 100))  # the opaque half keeps its colour
        assert max(r, g, b) < 60


def test_a_multi_page_pdf_is_sent_as_its_first_page_only():
    data, mime = model_input.prepare_for_model(_pdf(3), "application/pdf")
    assert mime == "application/pdf"
    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert len(pdf.pages) == 1


def test_a_one_page_pdf_is_sent_unchanged():
    original = _pdf(1)
    assert model_input.prepare_for_model(original, "application/pdf") == (
        original,
        "application/pdf",
    )


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "application/pdf"])
def test_bytes_that_do_not_decode_are_sent_as_they_are(mime, caplog):
    junk = b"\x89PNG not really" if mime == "image/png" else b"not a real file"
    assert model_input.prepare_for_model(junk, mime) == (junk, mime)
    assert "sending the original" in caplog.text


def test_other_types_and_empty_input_pass_through():
    assert model_input.prepare_for_model(b"x", "text/html") == (b"x", "text/html")
    assert model_input.prepare_for_model(b"", "image/jpeg") == (b"", "image/jpeg")
