import httpx

from scripts.verify_deployment import verify


def test_verify_accepts_the_atlas_backend_and_frontend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "components": {"api": "ok"}})
        if request.url.path == "/ready":
            return httpx.Response(
                200,
                json={"status": "ok", "components": {"api": "ok", "database": "ok", "qdrant": "ok"}},
            )
        if request.url.path == "/docs":
            return httpx.Response(200, text="<title>Project Atlas - Swagger UI</title>")
        if request.method == "OPTIONS" and request.url.path == "/projects":
            return httpx.Response(200, headers={"access-control-allow-origin": "https://atlas-epc.netlify.app"})
        return httpx.Response(200, text="<h1>Project Atlas</h1><p>EPC project intelligence</p>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = verify("https://api.example", "https://atlas-epc.netlify.app", client)

    assert all(result.ok for result in results)


def test_verify_rejects_the_unrelated_climate_site_and_degraded_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "components": {"api": "ok"}})
        if request.url.path == "/ready":
            return httpx.Response(503, json={"status": "degraded", "components": {"database": "error"}})
        if request.url.path == "/docs":
            return httpx.Response(200, text="Swagger UI")
        if request.method == "OPTIONS":
            return httpx.Response(200)
        return httpx.Response(200, text="Droughts Flooding Global Warming")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = verify("https://api.example", "https://atlas-epc.netlify.app", client)

    assert {result.name for result in results if not result.ok} == {"readiness", "cors", "frontend_identity"}
