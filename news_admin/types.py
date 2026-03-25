from __future__ import annotations

from typing import TypedDict


class NewsItem(TypedDict, total=False):
    title: str
    snippet: str
    url: str
    via: str
    thumbnail_url: str
    original_title: str
    original_snippet: str
