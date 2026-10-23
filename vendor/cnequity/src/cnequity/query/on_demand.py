from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from cnequity.adapters.eastmoney.em_auth import EastMoneyClient
from cnequity.adapters.eastmoney.stock_news import fetch_stock_news
from cnequity.config import Config
from cnequity.storage.atomic import write_json_atomic

logger = logging.getLogger(__name__)

# Datasets with a real fetch path. Stubs stay callable only so old configs get a
# clear NotImplementedError instead of an empty JSON that poisons the cache.
_IMPLEMENTED = frozenset({"stock_news", "research_reports"})


class OnDemandService:
    """Fetch high-churn per-symbol data on first query and cache locally."""

    def __init__(self, config: Config):
        self.config = config
        self.cache_root = config.meta_root / "on_demand"
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, dataset: str, symbol: str, **kwargs) -> Path:
        """Return a cache path that identifies the remote request.

        ``stock_news`` is parameterised by date, page size and sentiment mode.
        A symbol-only key would make a cached ``limit=5`` response satisfy a
        later ``limit=30`` request (and, worse, could return another date's
        headlines).  Keep the historical symbol-only path for the default
        request so existing caches remain useful, and use a short digest for
        non-default variants.
        """
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(symbol))
        request = self._cache_request(dataset, kwargs)
        suffix = ""
        if request:
            encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            suffix = "__" + hashlib.sha256(encoded).hexdigest()[:16]
        return self.cache_root / dataset / f"{safe}{suffix}.json"

    def _cache_request(self, dataset: str, kwargs: dict) -> dict[str, object]:
        """Canonicalise parameters that change the fetched payload."""
        if dataset != "stock_news":
            return {}
        on_date = kwargs.get("on_date")
        if hasattr(on_date, "isoformat"):
            on_date = on_date.isoformat()
        elif on_date is not None:
            on_date = str(on_date)
        request = {
            "on_date": on_date,
            "limit": int(kwargs.get("limit", 30)),
            "use_snownlp": bool(kwargs.get("use_snownlp", self.config.sentiment_use_snownlp)),
        }
        defaults = {
            "on_date": None,
            "limit": 30,
            "use_snownlp": bool(self.config.sentiment_use_snownlp),
        }
        return {} if request == defaults else request

    def fetch(self, dataset: str, symbol: str, **kwargs) -> dict:
        if not self.config.on_demand_enabled:
            raise ValueError("on-demand is disabled by [on_demand].enabled")
        if dataset not in self.config.on_demand_datasets and self.config.on_demand_datasets:
            raise ValueError(f"Dataset {dataset} not enabled for on-demand")
        if dataset not in _IMPLEMENTED:
            raise NotImplementedError(
                f"on-demand dataset {dataset!r} is not implemented "
                f"(implemented: {', '.join(sorted(_IMPLEMENTED))})."
            )

        path = self._cache_path(dataset, symbol, **kwargs)
        if path.exists() and not kwargs.get("refresh"):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                logger.warning("ignoring corrupt on-demand cache %s: %s", path, exc)

        payload = self._fetch_remote(dataset, symbol, **kwargs)
        if self._should_cache(payload):
            write_json_atomic(path, payload, ensure_ascii=False, indent=2)
        return payload

    @staticmethod
    def _should_cache(payload: dict) -> bool:
        if payload.get("status") == "not_implemented":
            return False
        if "error" in payload:
            return False
        return True

    def _fetch_remote(self, dataset: str, symbol: str, **kwargs) -> dict:
        if dataset == "stock_news":
            return self._fetch_stock_news(symbol, **kwargs)
        if dataset == "research_reports":
            return self._fetch_research_reports(symbol)
        if dataset in {"announcement_body", "financial_reports"}:
            raise NotImplementedError(
                f"{dataset} is not implemented yet; remove it from "
                "[on_demand].datasets (implemented: " + ", ".join(sorted(_IMPLEMENTED)) + ")."
            )
        raise NotImplementedError(
            f"on-demand dataset {dataset!r} is not implemented "
            f"(implemented: {', '.join(sorted(_IMPLEMENTED))})."
        )

    def _fetch_stock_news(self, symbol: str, **kwargs) -> dict:
        if not self.config.sources.get("eastmoney", True):
            raise RuntimeError("stock_news: eastmoney source disabled in config")
        on_date = kwargs.get("on_date")
        if isinstance(on_date, str):
            from datetime import date

            on_date = date.fromisoformat(on_date)
        limit = int(kwargs.get("limit", 30))
        use_snownlp = bool(kwargs.get("use_snownlp", self.config.sentiment_use_snownlp))
        payload = fetch_stock_news(
            symbol,
            on_date=on_date,
            limit=limit,
            use_snownlp=use_snownlp,
            config=self.config,
        )
        payload["data_version"] = "v1"
        payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
        return payload

    def _fetch_research_reports(self, symbol: str) -> dict:
        if not self.config.sources.get("eastmoney", True):
            raise RuntimeError("research_reports: eastmoney source disabled in config")
        code = symbol.split(".")[0]
        url = f"https://reportapi.eastmoney.com/report/list?code={code}&pageSize=10"
        try:
            with EastMoneyClient(config=self.config) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()
                return {"symbol": symbol, "items": data, "source": "eastmoney"}
        except Exception as exc:
            logger.warning("research_reports fetch failed: %s", exc)
            return {"symbol": symbol, "items": [], "error": str(exc), "source": "eastmoney"}
