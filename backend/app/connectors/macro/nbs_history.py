"""历史回填的公开报告身份和共享契约；不猜测 URL，不在配置中保存数值。"""
from __future__ import annotations

from datetime import date
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.connectors.macro.contracts import ProviderObservation, ProviderSchemaError
from app.connectors.macro.nbs_release_feed import (
    _PAGINATION, _ReleaseLinks, _compact, _parse, _period, _release_url_allowed, _title_match,
)
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition


SERIES_IDS = ('cn.cpi.core_yoy', 'cn.industry.value_added_yoy')
DEFAULT_RELEASES_FILE = Path(__file__).resolve().parents[3] / 'config' / 'nbs-history-releases.json'


class NbsReleaseLocator(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, populate_by_name=True)

    series_id: Literal['cn.cpi.core_yoy', 'cn.industry.value_added_yoy'] = Field(alias='seriesId')
    url: str = Field(max_length=512)
    title: str = Field(max_length=256)

    @model_validator(mode='after')
    def validate_identity(self) -> Self:
        if not _release_url_allowed(self.url) or self.title != _compact(self.title):
            raise ValueError('NBS_HISTORY_IDENTITY_INVALID')
        match = _title_match(self.series_id, self.title)
        if match is None:
            raise ValueError('NBS_HISTORY_IDENTITY_INVALID')
        _period(match)
        return self

    @property
    def observed_at(self) -> date:
        match = _title_match(self.series_id, self.title)
        if match is None:  # model 验证后不能进入。
            raise ValueError('NBS_HISTORY_IDENTITY_INVALID')
        return _period(match)

    @property
    def key(self) -> str:
        return f'{self.series_id}:{self.url}'


class _Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal['1.0'] = Field(alias='schemaVersion')
    releases: tuple[NbsReleaseLocator, ...] = Field(max_length=10000)


def load_history_releases(path: Path) -> tuple[NbsReleaseLocator, ...]:
    try:
        return _Manifest.model_validate_json(path.read_bytes()).releases
    except (OSError, ValueError):
        raise ValueError('NBS_HISTORY_MANIFEST_INVALID') from None


def discover_history_page(body: str, page: int, start: date, end: date, *,
                          known_releases: tuple[NbsReleaseLocator, ...] = ()
                          ) -> tuple[int, tuple[NbsReleaseLocator, ...], str]:
    pagination = _PAGINATION.search(body)
    if (pagination is None or int(pagination[2]) != page
            or not 1 <= int(pagination[1]) <= 1000 or page >= int(pagination[1])):
        raise ProviderSchemaError('NBS_INDEX_SCHEMA_INVALID')
    parser = _ReleaseLinks()
    parser.feed(body)
    known_titles: dict[str, set[str]] = {}
    for release in known_releases:
        known_titles.setdefault(release.url, set()).add(release.title)
    releases = []
    for url, title in parser.links.items():
        # 未识别的新标题也不能抹掉已知报告的身份。
        if url in known_titles and known_titles[url] != {title}:
            raise ProviderSchemaError('NBS_HISTORY_MANIFEST_CONFLICT')
        for series_id in SERIES_IDS:
            if _title_match(series_id, title) is None:
                continue
            try:
                item = NbsReleaseLocator(series_id=series_id, url=url, title=title)
            except ValueError:
                raise ProviderSchemaError('NBS_HISTORY_IDENTITY_INVALID') from None
            if start <= item.observed_at <= end:
                releases.append(item)
    # 所有目录链接参与指纹，目标范围以外的新链接也会暴露分页漂移。
    identity = json.dumps(sorted(parser.links.items()), ensure_ascii=False, separators=(',', ':'))
    return int(pagination[1]), tuple(releases), sha256(identity.encode()).hexdigest()


class _LegacyIdentityParser(HTMLParser):
    """只读取已核对的旧版 detail-title 区域，不从正文任意位置猜发布日期。"""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []
        self.page_titles: list[str] = []
        self.headings: list[str] = []
        self.dates: list[str] = []
        self.metadata_names: set[str] = set()
        self._target: tuple[str, int] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'meta':
            self.metadata_names.add((dict(attrs).get('name') or '').casefold())
        if tag in {'meta', 'link', 'img', 'br', 'hr', 'input', 'wbr', 'source', 'area', 'base', 'embed', 'param', 'track', 'col'}:
            return
        self.stack.append((tag, set((dict(attrs).get('class') or '').split())))
        classes = set().union(*(item[1] for item in self.stack))
        target = ('title' if tag == 'title' else
                  'heading' if tag == 'h1' and 'detail-title' in classes else
                  'date' if tag == 'p' and {'detail-title', 'detail-title-des'} <= classes
                  and not dict(attrs).get('class')
                  and any(item[0] == 'h2' for item in self.stack) else None)
        if target is not None:
            self._target, self._text = (target, len(self.stack)), []

    def handle_data(self, data: str) -> None:
        if self._target is not None and not any(tag in {'script', 'style'} for tag, _ in self.stack):
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or tag not in [item[0] for item in self.stack]:
            return
        index = max(i for i, item in enumerate(self.stack) if item[0] == tag)
        if self._target is not None and index + 1 <= self._target[1]:
            kind, _ = self._target
            value = ''.join(self._text)
            {'title': self.page_titles, 'heading': self.headings, 'date': self.dates}[kind].append(value)
            self._target = None
        del self.stack[index:]


def _legacy_metadata(locator: NbsReleaseLocator, body: str) -> str:
    legacy = _LegacyIdentityParser()
    legacy.feed(body)
    if legacy.metadata_names & {'articletitle', 'pubdate'}:
        return body  # 不用旧版备选元数据掩盖部分缺失/冲突的新契约。
    if re.match(r'^https://www\.stats\.gov\.cn/sj/zxfb/202[34](?:0[1-9]|1[0-2])/', locator.url) is None:
        return body  # 已核对迁移页与2024历史DOM；最新页仍要求原meta契约。
    titles = {_compact(value).removesuffix('-国家统计局') for value in legacy.page_titles}
    headings = {_compact(value) for value in legacy.headings}
    dates = {' '.join(value.split()) for value in legacy.dates}
    if titles != {locator.title} or headings != {locator.title} or len(dates) != 1:
        raise ProviderSchemaError('NBS_HISTORY_LEGACY_IDENTITY_INVALID')
    publication = next(iter(dates))
    if re.fullmatch(r'\d{4}/\d{2}/\d{2} \d{2}:\d{2}', publication) is None:
        raise ProviderSchemaError('NBS_HISTORY_LEGACY_IDENTITY_INVALID')
    return (f'<meta name="ArticleTitle" content="{escape(locator.title, quote=True)}">'
            f'<meta name="PubDate" content="{publication}">' + body)


def parse_history_release(locator: NbsReleaseLocator, source: MacroSourceDefinition,
                          body: str) -> ProviderObservation:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(locator.series_id)
    return _parse(series, source, locator.url, _legacy_metadata(locator, body), locator.title)
