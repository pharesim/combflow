"""Shared text-cleaning utilities.

Zero external dependencies (stdlib only) so both the worker and seed
scripts can import without pulling in heavy libraries like nectar.
"""

import re

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]+\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+")
_MD_HEADER = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_DIVIDER = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_MULTI_SPACE = re.compile(r"\s{2,}")


def clean_post_body(text: str) -> str:
    """Strip markdown images, links, HTML, URLs -- keep only meaningful words."""
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = _URL.sub("", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_DIVIDER.sub("", text)
    text = _MULTI_SPACE.sub(" ", text)
    return text.strip()
