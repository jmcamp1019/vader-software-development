"""Official House Clerk filing index — the integrity anchor for house ingest.

Per ADR-001, mirror trades are only inserted when their filing DocID appears
in the Clerk's official yearly index ({YEAR}FD.zip, containing a tab-separated
index of every filing). The official bulk archive has usable PTR coverage from
2015 onward; older mirror rows remain quarantined as legacy-unindexed. A
supported year whose index is missing upstream (HTTP 404) yields an empty
DocID set, so that year's trades quarantine; any other fetch failure propagates
so the caller can fail closed.
"""
from __future__ import annotations

import io
import re
import urllib.error
import urllib.request
import zipfile
from typing import Any, Iterable

from . import config

_PTR_YEAR_PATTERN = re.compile(r"/(?:ptr|financial)-pdfs/(\d{4})/")
CLERK_PTR_INDEX_START_YEAR = 2015


def parse_index_doc_ids(zip_payload: bytes) -> set[str]:
    """Extract every filing DocID from a Clerk {YEAR}FD.zip index payload."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_payload)) as archive:
            txt_names = [n for n in archive.namelist() if n.lower().endswith(".txt")]
            if not txt_names:
                raise ValueError("Clerk index ZIP contains no .txt index file")
            text = archive.read(txt_names[0]).decode("utf-8-sig")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Clerk index payload is not a ZIP archive: {exc}") from exc

    lines = text.splitlines()
    if not lines:
        raise ValueError("Clerk index file is empty")
    header = [column.strip() for column in lines[0].split("\t")]
    try:
        doc_id_column = header.index("DocID")
    except ValueError:
        raise ValueError(f"Clerk index missing DocID column, header={header!r}") from None

    doc_ids: set[str] = set()
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) <= doc_id_column:
            continue
        doc_id = fields[doc_id_column].strip()
        if doc_id:
            doc_ids.add(doc_id)
    return doc_ids


def fetch_index_doc_ids(year: int, timeout: int | None = None) -> set[str]:
    """Fetch the official Clerk index for one filing year.

    Returns an empty set on HTTP 404 (no index published for that year — the
    caller quarantines those trades); every other error propagates so the
    ingest fails closed rather than inserting unanchored data.
    """
    url = config.CLERK_HOUSE_INDEX_URL_TEMPLATE.format(year=year)
    request = urllib.request.Request(url, headers={"User-Agent": config.CLERK_USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=timeout or config.HTTP_TIMEOUT_SECONDS
        ) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return set()
        raise
    return parse_index_doc_ids(payload)


def filing_year(record: dict[str, Any]) -> int | None:
    """Best-effort filing year of a mirror record (PDF URL path, else disclosure year)."""
    url = str(record.get("source_url") or record.get("ptr_link") or "")
    match = _PTR_YEAR_PATTERN.search(url)
    if match:
        return int(match.group(1))
    raw_date = str(record.get("disclosure_date") or "")
    year_match = re.search(r"(\d{4})", raw_date)
    return int(year_match.group(1)) if year_match else None


def is_legacy_unindexed_record(record: dict[str, Any]) -> bool:
    """True only for years before the official bulk index carries PTRs."""
    year = filing_year(record)
    return year is not None and year < CLERK_PTR_INDEX_START_YEAR


def fetch_doc_ids_for_records(
    records: Iterable[dict[str, Any]], timeout: int | None = None
) -> set[str]:
    """Fetch official DocIDs for years with supported bulk PTR coverage."""
    years = {
        year
        for year in (filing_year(record) for record in records)
        if year is not None and year >= CLERK_PTR_INDEX_START_YEAR
    }
    doc_ids: set[str] = set()
    for year in sorted(years):
        doc_ids |= fetch_index_doc_ids(year, timeout=timeout)
    return doc_ids
