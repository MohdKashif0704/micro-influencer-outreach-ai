"""Meta Creator Discovery API client for the EDXSO influencer outreach assignment.

Requires eligible Meta Creator Marketplace access and an appropriate access token.
This client does not scrape Instagram or bypass Meta permissions.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests


GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v25.0")
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class MetaAPIError(RuntimeError):
    """Raised when Meta returns an API error or an unsuccessful HTTP status."""


@dataclass
class MetaConfig:
    access_token: str
    ig_user_id: str
    api_version: str = GRAPH_API_VERSION
    timeout_seconds: int = 30
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "MetaConfig":
        token = os.getenv("META_ACCESS_TOKEN", "").strip()
        ig_user_id = os.getenv("META_IG_USER_ID", "").strip()
        if not token or not ig_user_id:
            raise ValueError(
                "Set META_ACCESS_TOKEN and META_IG_USER_ID in your environment."
            )
        return cls(
            access_token=token,
            ig_user_id=ig_user_id,
            api_version=os.getenv("META_GRAPH_API_VERSION", "v25.0"),
        )


def _json_param(value: Any) -> str:
    """Meta list-valued filters are sent as JSON strings."""
    return json.dumps(value, separators=(",", ":"))


class MetaCreatorDiscoveryClient:
    def __init__(self, config: MetaConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.base_url = f"https://graph.facebook.com/{config.api_version}"
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "access_token": self.config.access_token}
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.get(
                    url, params=params, timeout=self.config.timeout_seconds
                )
                # Retry transient rate-limit/server errors only.
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.config.max_retries:
                        time.sleep(min(2 ** attempt, 8))
                        continue
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"message": response.text[:500]}

                if not response.ok or "error" in payload:
                    error = payload.get("error", payload)
                    message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    code = error.get("code") if isinstance(error, dict) else None
                    raise MetaAPIError(
                        f"Meta API request failed (HTTP {response.status_code}, "
                        f"code={code}): {message}"
                    )
                return payload
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise MetaAPIError(f"Meta API connection failed: {exc}") from exc
        raise MetaAPIError(f"Meta API request failed: {last_error}")

    @staticmethod
    def _prepare_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
        """Convert list filters to the JSON-encoded form expected by Meta."""
        prepared: dict[str, Any] = {}
        list_keys = {
            "creator_countries", "creator_age_bucket", "creator_interests",
            "creator_gender", "creator_states", "major_audience_age_bucket",
            "major_audience_countries", "major_audience_gender",
            "major_audience_states", "major_audience_device_type",
            "creator_language", "similar_to_creators",
        }
        for key, value in (filters or {}).items():
            if value is None or value == "":
                continue
            prepared[key] = _json_param(value) if key in list_keys and not isinstance(value, str) else value
        return prepared

    def search_creators(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
        fields: Iterable[str] | None = None,
        page_size: int = 50,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """Search marketplace creators and follow cursor pagination where returned.

        Note: the API only returns creators visible to the authenticated eligible brand.
        A request may return fewer records than max_results.
        """
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100.")
        if max_results < 1:
            return []

        params = self._prepare_filters(filters)
        if query:
            params["query"] = query

        requested_fields = list(fields or [
            "id", "username", "biography", "country", "email", "category",
            "portfolio_url", "onboarded_status", "platforms",
        ])
        params["fields"] = ",".join(requested_fields)
        params["limit"] = min(page_size, max_results)

        url = f"{self.base_url}/{self.config.ig_user_id}/creator_marketplace_creators"
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        while url and len(results) < max_results:
            payload = self._get(url, params)
            for creator in payload.get("data", []):
                creator_id = str(creator.get("id", ""))
                if creator_id and creator_id in seen_ids:
                    continue
                if creator_id:
                    seen_ids.add(creator_id)
                results.append(creator)
                if len(results) >= max_results:
                    break

            paging = payload.get("paging", {})
            next_url = paging.get("next")
            if not next_url:
                break
            # Graph API's paging.next URL already includes its cursor and query params.
            # Keep access token explicitly added by _get; do not reuse first-page params.
            url = next_url
            params = {}
        return results[:max_results]

    def enrich_creator(self, username: str) -> dict[str, Any] | None:
        """Fetch one creator's profile, available metrics, and recent media.

        Missing fields are left missing; the normalizer labels them honestly.
        """
        fields = (
            "id,username,biography,country,gender,age_bucket,email,category,"
            "portfolio_url,onboarded_status,platforms,"
            "insights.metrics(total_followers).period(overall),"
            "insights.metrics(reels_interaction_rate).period(overall).time_range(last_90_days),"
            "insights.metrics(creator_reach).period(overall).time_range(this_month),"
            "recent_media.limit(10){id,caption,creation_time,permalink,media_type,"
            "insights.metrics(likes,comments,views,shares)}"
        )
        url = f"{self.base_url}/{self.config.ig_user_id}/creator_marketplace_creators"
        payload = self._get(url, {"username": username, "fields": fields})
        creators = payload.get("data", [])
        return creators[0] if creators else None


def _insight_value(creator: dict[str, Any], metric_name: str) -> Any:
    insights = creator.get("insights", {})
    for item in insights.get("data", []) if isinstance(insights, dict) else []:
        if item.get("name") == metric_name:
            total = item.get("total_value", {})
            if isinstance(total, dict):
                return total.get("value")
    return None


def normalize_creator(creator: dict[str, Any]) -> dict[str, Any]:
    """Map API response into assignment-friendly fields without inventing values."""
    username = creator.get("username")
    follower_count = _insight_value(creator, "total_followers")
    engagement_rate = _insight_value(creator, "reels_interaction_rate")
    recent_media = creator.get("recent_media", {}).get("data", [])
    content_themes = []
    recent_content = []

    for media in recent_media:
        caption = (media.get("caption") or "").strip()
        if caption:
            # Keep short evidence snippets for review/personalization.
            content_themes.append(caption[:180])
        recent_content.append({
            "caption": caption or "Not Found",
            "url": media.get("permalink") or "Not Found",
            "published_at": media.get("creation_time") or "Not Found",
            "media_type": media.get("media_type") or "Not Found",
        })

    return {
        "influencer_id": creator.get("id") or "Not Found",
        "name": username or "Not Found",
        "platform": "Instagram",
        "profile_url": f"https://www.instagram.com/{username}/" if username else "Not Found",
        "follower_count": follower_count if follower_count is not None else "Unavailable",
        "engagement_rate": engagement_rate if engagement_rate is not None else "Unavailable",
        "category_niche": creator.get("category") or "Not Found",
        "bio": creator.get("biography") or "Not Found",
        "contact_email": creator.get("email") or "Not Found",
        "website": creator.get("portfolio_url") or "Not Found",
        "creator_country": creator.get("country") or "Unavailable",
        "creator_gender": creator.get("gender") or "Unavailable",
        "creator_age_bucket": creator.get("age_bucket") or "Unavailable",
        "onboarded_status": creator.get("onboarded_status") or "Unavailable",
        "platforms": creator.get("platforms") or ["Instagram"],
        "content_themes": content_themes or ["Not Found"],
        "recent_content": recent_content,
        "source": "Meta Creator Discovery API",
    }


def classify_creator(
    creator: dict[str, Any],
    min_followers: int = 5_000,
    max_followers: int = 100_000,
    niche_keywords: tuple[str, ...] = ("technology", "science", "education", "ai", "tech"),
) -> dict[str, Any]:
    """Apply transparent rules and return pass/fail plus reasons."""
    normalized = normalize_creator(creator)
    reasons: list[str] = []
    follower_count = _insight_value(creator, "total_followers")
    searchable_text = " ".join([
        str(creator.get("username", "")),
        str(creator.get("biography", "")),
        str(creator.get("category", "")),
        " ".join(normalized["content_themes"]),
    ]).lower()

    if follower_count is None:
        reasons.append("Follower count unavailable; cannot verify micro-influencer range.")
    elif not (min_followers <= int(follower_count) <= max_followers):
        reasons.append(f"Follower count outside {min_followers:,}–{max_followers:,} range.")

    if not any(keyword.lower() in searchable_text for keyword in niche_keywords):
        reasons.append("No configured niche keyword found in available profile/content evidence.")

    normalized["filter_status"] = "PASS" if not reasons else "FAIL"
    normalized["filter_reasons"] = reasons if reasons else ["Meets configured follower and niche rules."]
    return normalized


def export_csv(records: list[dict[str, Any]], output_path: str = "data/influencers.csv") -> str:
    """Export records to CSV; nested values are JSON-encoded for portability."""
    import csv
    from pathlib import Path

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        path.write_text("", encoding="utf-8")
        return str(path)

    fieldnames = list(dict.fromkeys(key for row in records for key in row.keys()))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in records:
            writer.writerow({
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                for key, value in row.items()
            })
    return str(path)
