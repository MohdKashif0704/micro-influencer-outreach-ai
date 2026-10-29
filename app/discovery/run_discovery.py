"""Example CLI: search creators, enrich available metrics, classify, export CSV."""
import argparse
import json
from pathlib import Path

from app.meta_creator_client import (
    MetaConfig, MetaCreatorDiscoveryClient, classify_creator, export_csv
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="technology")
    parser.add_argument("--country", default="IN")
    parser.add_argument("--min-followers", type=int, default=5_000)
    parser.add_argument("--max-followers", type=int, default=100_000)
    parser.add_argument("--max-results", type=int, default=50)
    parser.add_argument("--output", default="data/influencers.csv")
    args = parser.parse_args()

    client = MetaCreatorDiscoveryClient(MetaConfig.from_env())
    filters = {
        "creator_countries": [args.country],
        "creator_min_followers": 0,
        "creator_max_followers": 100000,
        "has_public_contact_email": False,
        "platform": "instagram",
    }

    creators = client.search_creators(
        query=args.query,
        filters=filters,
        max_results=args.max_results,
        fields=[
            "id", "username", "biography", "country", "email", "category",
            "portfolio_url", "onboarded_status", "platforms",
            "insights.metrics(total_followers).period(overall)",
            "insights.metrics(reels_interaction_rate).period(overall).time_range(last_90_days)",
            "recent_media.limit(10){id,caption,creation_time,permalink,media_type}",
        ],
    )

    records = []
    for creator in creators:
        username = creator.get("username")
        # Enrichment is best-effort; search response remains usable if one profile fails.
        if username:
            try:
                enriched = client.enrich_creator(username)
                if enriched:
                    creator = enriched
            except Exception as exc:
                creator["_enrichment_error"] = str(exc)

        record = classify_creator(
            creator,
            min_followers=args.min_followers,
            max_followers=args.max_followers,
            niche_keywords=("technology", "science", "education", "ai", "tech"),
        )
        if creator.get("_enrichment_error"):
            record["enrichment_error"] = creator["_enrichment_error"]
        records.append(record)

    output = export_csv(records, args.output)
    passed = sum(row["filter_status"] == "PASS" for row in records)
    print(json.dumps({
        "discovered": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "csv": output,
    }, indent=2))


if __name__ == "__main__":
    main()
