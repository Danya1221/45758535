"""Render a short-lived Telegram login token in memory, never to disk."""
from io import BytesIO

import qrcode
from qrcode.image.pure import PyPNGImage


def qr_png(url: str) -> bytes:
    buffer = BytesIO()
    image = qrcode.make(url, image_factory=PyPNGImage, box_size=8, border=4)
    image.save(buffer)
    return buffer.getvalue()
