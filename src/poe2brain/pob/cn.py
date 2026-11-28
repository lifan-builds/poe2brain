# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from poe2brain.config import CN_IMPORT_HELP


def cn_export_name(url: str) -> str | None:
    target = urlsplit(url)
    if target.hostname in {"wegame.com.cn", "www.wegame.com.cn"}:
        raise ValueError(CN_IMPORT_HELP)
    if target.hostname == "poe2db.tw" and target.path.rstrip("/") == "/cn/qq_build":
        name = parse_qs(target.query).get("name")
        if not name:
            raise ValueError(CN_IMPORT_HELP)
        return name[0]
    return None


def cn_export_url(name: str) -> str:
    return "https://poe2db.tw/cn/qq_build?" + urlencode({"name": name})


class _ExportPage(HTMLParser):
    code = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            attr = dict(attrs)
            if attr.get("id") == "pobCode":
                self.code = attr.get("value") or ""


def read_cn_export_code(html: str) -> str:
    page = _ExportPage()
    page.feed(html)
    if not page.code:
        raise ValueError(f"此角色页没有可导入的 PoB Code。{CN_IMPORT_HELP}")
    return page.code
