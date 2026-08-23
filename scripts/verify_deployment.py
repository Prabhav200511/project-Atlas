import argparse
import json
from dataclasses import asdict, dataclass

import httpx


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def verify(api_url: str, frontend_url: str, client: httpx.Client) -> list[CheckResult]:
    api_url = api_url.rstrip("/")
    frontend_url = frontend_url.rstrip("/")
    results: list[CheckResult] = []

    health = client.get(f"{api_url}/health", timeout=20)
    health_body = health.json() if health.headers.get("content-type", "").startswith("application/json") else {}
    results.append(
        CheckResult("liveness", health.status_code == 200 and health_body.get("status") == "ok", str(health.status_code))
    )

    ready = client.get(f"{api_url}/ready", timeout=20)
    ready_body = ready.json() if ready.headers.get("content-type", "").startswith("application/json") else {}
    components = ready_body.get("components", {})
    ready_ok = ready.status_code == 200 and all(
        components.get(name) == "ok" for name in ("api", "database", "qdrant")
    )
    results.append(CheckResult("readiness", ready_ok, json.dumps(components, sort_keys=True)))

    docs = client.get(f"{api_url}/docs", timeout=20)
    results.append(CheckResult("docs", docs.status_code == 200 and "Swagger UI" in docs.text, str(docs.status_code)))

    cors = client.options(
        f"{api_url}/projects",
        headers={"Origin": frontend_url, "Access-Control-Request-Method": "GET"},
        timeout=20,
    )
    allowed_origin = cors.headers.get("access-control-allow-origin")
    results.append(CheckResult("cors", allowed_origin == frontend_url, allowed_origin or "missing"))

    frontend = client.get(frontend_url, timeout=20)
    identity_ok = (
        frontend.status_code == 200
        and "Project Atlas" in frontend.text
        and "EPC project intelligence" in frontend.text
    )
    results.append(CheckResult("frontend_identity", identity_ok, str(frontend.status_code)))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the deployed Project Atlas identity and dependencies.")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--frontend-url", required=True)
    args = parser.parse_args()
    with httpx.Client(follow_redirects=True) as client:
        results = verify(args.api_url, args.frontend_url, client)
    print(json.dumps([asdict(result) for result in results], indent=2))
    raise SystemExit(0 if all(result.ok for result in results) else 1)


if __name__ == "__main__":
    main()
