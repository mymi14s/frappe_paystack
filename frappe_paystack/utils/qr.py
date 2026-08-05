"""QR codes for Paystack checkout links, written as SVG."""

from base64 import b64encode
from io import BytesIO

import pyqrcode

# Pixels per QR module.
QR_SCALE = 4

# Blank modules around the code.
QR_QUIET_ZONE = 4

SVG_DATA_URI_PREFIX = "data:image/svg+xml;base64,"


def qr_svg(text: str) -> str:
    """Return an SVG document encoding text."""
    stream = BytesIO()
    pyqrcode.create(text).svg(
        stream,
        scale=QR_SCALE,
        quiet_zone=QR_QUIET_ZONE,
        background="#ffffff",
        module_color="#000000",
    )
    return stream.getvalue().decode().replace("\n", "")


def qr_data_uri(text: str) -> str:
    """Return an SVG QR code as a data URI an <img> can render."""
    if not text:
        return ""

    return SVG_DATA_URI_PREFIX + b64encode(qr_svg(text).encode()).decode()
