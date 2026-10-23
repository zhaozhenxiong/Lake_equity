"""Locate a dated window in TDX's reverse-offset history without a calendar guess."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime
from typing import TypeVar

Page = TypeVar("Page")


def window_pages(
    fetch: Callable[[int], Page],
    keys: Callable[[Page], Sequence[date | datetime | None]],
    start: date,
    end: date,
    *,
    page_size: int,
    page_limit: int,
    seek: bool,
    strict_limit: bool,
    detect_repeats: bool,
    error: type[RuntimeError],
    label: str,
    limit_message: str,
    on_page: Callable[[], None] | None = None,
) -> Iterator[Page]:
    """Yield only window pages; cache probes for the duration of this fetch.

    Full pages newer than ``end`` are bracketed exponentially, then bisected.
    Short/empty pages bound the search too: never jump past a short oldest page.
    ``page_limit`` bounds history depth, not just the number of probe requests.
    Bad date keys disable seeking; the ordinary sequential parser still decides
    which rows are usable. Every request, including tip verification, uses fetch.
    """
    cache: dict[int, Page] = {}
    identities: dict[int, tuple] = {}
    bounds: dict[int, tuple[date, date] | None] = {}
    seen: set[tuple] = set()
    requests = 0
    jumped = False
    searching = False
    # The standard bars wire request encodes start as an unsigned 16-bit value.
    last_wire_page = 65535 // page_size

    def request(index: int) -> Page:
        nonlocal requests
        if index > last_wire_page:
            raise error(f"{label} history offset exceeds the TDX wire limit")
        if requests and on_page is not None:
            on_page()
        requests += 1
        return fetch(index * page_size)

    def read(index: int) -> Page:
        nonlocal jumped
        if index in cache:
            return cache[index]
        jumped |= bool(cache) and index > max(cache) + 1
        page = request(index)
        identity = tuple(keys(page))
        if identity and (detect_repeats or searching) and identity in seen:
            raise error(f"{label} pagination did not advance at start={index * page_size}")
        if identity:
            seen.add(identity)
        dates = [k.date() if isinstance(k, datetime) else k for k in identity if k is not None]
        bound = (min(dates), max(dates)) if dates and len(dates) == len(identity) else None
        if searching and bound is not None:
            for other, other_bound in bounds.items():
                if other_bound is None:
                    continue
                newer, older = (other_bound, bound) if other < index else (bound, other_bound)
                if newer[0] < older[1]:
                    raise error(f"{label} history pages are out of date order")
        cache[index], identities[index], bounds[index] = page, identity, bound
        return page

    class UnknownDates(Exception):
        pass

    def newer_than_window(index: int) -> bool:
        read(index)
        identity = identities[index]
        if identity and bounds[index] is None:
            raise UnknownDates
        return len(identity) == page_size and bounds[index][0] > end

    index = 0
    if page_limit > 0:
        read(0)
        if seek and bounds[0] is not None and newer_than_window(0):
            searching = True
            last = min(page_limit - 1, last_wire_page)
            low, high = 0, min(1, last)
            try:
                while high < last and newer_than_window(high):
                    low, high = high, min(high * 2, last)
                # Also load the capped upper bound before bisecting.
                newer_than_window(high)
                while high - low > 1:
                    middle = (low + high) // 2
                    if newer_than_window(middle):
                        low = middle
                    else:
                        high = middle
                index = high
            except UnknownDates:
                index = 0
            searching = False

    while index < page_limit:
        page = read(index)
        identity = identities[index]
        if not identity:
            break
        yield page
        dates = [k.date() if isinstance(k, datetime) else k for k in identity if k is not None]
        if (dates and min(dates) < start) or len(identity) < page_size:
            break
        index += 1
    else:
        if strict_limit:
            raise error(limit_message)

    if jumped and tuple(keys(request(0))) != identities[0]:
        # Relative offsets move when a new bar arrives. Do not publish a window
        # assembled across two positions of the tip. Prices/volumes may change
        # without changing positions, so compare timestamps only.
        raise error(f"{label} history tip changed during date seeking; retry the window")
