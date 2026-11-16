"""External scheduler client; does not create a new Railway service or enable a cron."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from analyzer.models import ItemsRequest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--items-file", type=Path, help="明确标注为自选清单，不冒充目标榜单")
    args = parser.parse_args()
    base = os.environ["ANALYZER_BASE_URL"].rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise SystemExit("ANALYZER_BASE_URL must be an HTTPS URL without embedded credentials")
    headers = {"X-Access-Key": os.environ["ANALYZER_ACCESS_KEY"]}
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        if args.items_file:
            body = ItemsRequest.model_validate_json(args.items_file.read_text()).model_dump(mode="json")
            response = client.post(base + "/api/analyze/items", headers=headers, json=body)
        else:
            response = client.get(base + "/api/analyze/top30", headers=headers)
    if response.status_code not in (200, 202):
        raise SystemExit(
            f"Analysis unavailable: HTTP {response.status_code}; consult authenticated capabilities/diagnostic"
        )
    print(json.dumps(response.json(), ensure_ascii=False))


if __name__ == "__main__":
    main()
