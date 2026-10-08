from io import BytesIO
import random

from PIL import Image
import pytest
import requests

from src.notifier.telegram_chart import build_scene, compact_png, render_png
from src.notifier.telegram_transport import PHOTO_REQUEST_MAX_BYTES, TelegramTransport
from tests.support.telegram import demo_event


@pytest.mark.parametrize("stage", ["plan", "stop", "final"])
def test_chart_fits_multipart_with_long_russian_caption(stage):
    photo = render_png(build_scene(demo_event(stage), 3))
    data = {"caption": "Я" * 1024, "parse_mode": "HTML",
            "reply_markup": '{"inline_keyboard":[[{"text":"Открыть сделку","url":"https://t.me/example/123"}]]}'}
    transport = TelegramTransport("https://proxy.test", "secret", "-10012345")
    compressed = compact_png(photo, transport.photo_budget(data))
    prepared = requests.Request("POST", "https://proxy.test/sendPhoto",
                                data={"chat_id": transport.chat_id, **data},
                                files={"photo": ("trade.png", compressed, "image/png")}).prepare()
    assert len(prepared.body) <= PHOTO_REQUEST_MAX_BYTES
    with Image.open(BytesIO(compressed)) as image, Image.open(BytesIO(photo)) as original:
        assert image.format == "PNG"
        assert image.width >= 800
        assert abs(image.width / image.height - original.width / original.height) < .01
        colors = [rgb for _, rgb in image.convert("RGB").getcolors(image.width * image.height)]
        assert any(r > 1.5 * g for r, g, b in colors)  # Красный стоп.
        assert any(g > 1.5 * r for r, g, b in colors)  # Зелёные цели.
        assert any(b > 1.5 * r and b > g for r, g, b in colors)  # Синий вход.
    assert compact_png(compressed, transport.photo_budget(data)) == compressed


def test_uncompressible_image_is_rejected_instead_of_tiny_chart():
    image = Image.frombytes("RGB", (800, 600), random.Random(17).randbytes(800 * 600 * 3))
    stream = BytesIO()
    image.save(stream, format="PNG")
    with pytest.raises(ValueError, match="800"):
        compact_png(stream.getvalue(), 12_000)
