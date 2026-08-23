from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_render_blueprint_has_atlas_production_contract() -> None:
    blueprint = (ROOT / "render.yaml").read_text()

    for required in (
        "name: project-atlas-api",
        "runtime: python",
        "buildCommand: pip install .",
        "startCommand: bash ./scripts/start_production.sh",
        "healthCheckPath: /ready",
        "key: DATABASE_URL",
        "key: QDRANT_URL",
        "key: QDRANT_API_KEY",
        "key: FRONTEND_URL",
    ):
        assert required in blueprint


def test_netlify_builds_the_nextjs_application_from_frontend() -> None:
    config = (ROOT / "netlify.toml").read_text()

    assert 'base = "frontend"' in config
    assert 'command = "npm run build"' in config
    assert 'publish = ".next"' in config
    assert 'package = "@netlify/plugin-nextjs"' in config


def test_production_start_binds_render_port_and_requires_dependencies() -> None:
    script = (ROOT / "scripts" / "start_production.sh").read_text()

    for required in (
        ': "${DATABASE_URL:?DATABASE_URL is required}"',
        ': "${QDRANT_URL:?QDRANT_URL is required}"',
        ': "${QDRANT_API_KEY:?QDRANT_API_KEY is required}"',
        ': "${FRONTEND_URL:?FRONTEND_URL is required}"',
        ': "${PORT:?PORT is required}"',
        "alembic upgrade head",
        "--host 0.0.0.0",
        '--port "$PORT"',
        '--workers "${WEB_CONCURRENCY:-1}"',
    ):
        assert required in script
