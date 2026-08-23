from pathlib import Path

import httpx
import pytest

from scripts.seed_demo import should_upload


def test_default_seed_skips_existing_documents() -> None:
    existing = {"UPS_Specification.md"}

    assert should_upload("UPS_Specification.md", existing, False) is False


def test_reupload_includes_existing_documents() -> None:
    existing = {"UPS_Specification.md"}

    assert should_upload("UPS_Specification.md", existing, True) is True


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("POST", "http://atlas.test/documents")
        self.response = httpx.Response(status_code, request=self.request, json=payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        self.response.raise_for_status()


class RestartableUploadClient:
    def __init__(self, states: dict[str, str], fail_once: str | None = None) -> None:
        self.states = states
        self.fail_once = fail_once
        self.requests: list[str] = []

    def post(self, _: str, *, data: dict, files: dict) -> FakeResponse:
        assert data["document_type"]
        filename = files["file"][0]
        self.requests.append(filename)
        if filename == self.fail_once:
            self.fail_once = None
            return FakeResponse(503, {"error": {"code": "unavailable"}})
        state = self.states[filename]
        if state == "healthy":
            return FakeResponse(409, {"error": {"code": "duplicate_document"}})
        if state == "mismatch":
            return FakeResponse(409, {"error": {"code": "document_repair_mismatch"}})
        self.states[filename] = "healthy"
        return FakeResponse(201, {"ingestion": {"status": "completed"}})


def write_sources(tmp_path: Path, names: list[str]) -> list[tuple[str, Path]]:
    result = []
    for name in names:
        path = tmp_path / name
        path.write_text(f"# {name}\n")
        result.append(("RFI", path))
    return result


def test_reupload_continues_from_healthy_identical_document_to_missing_document(tmp_path: Path) -> None:
    from scripts.seed_demo import upload_sources

    source_list = write_sources(tmp_path, ["healthy.md", "missing.md"])
    client = RestartableUploadClient({"healthy.md": "healthy", "missing.md": "missing"})

    upload_sources(client, "project-id", source_list, {"healthy.md", "missing.md"}, reupload=True)
    upload_sources(client, "project-id", source_list, {"healthy.md", "missing.md"}, reupload=True)

    assert client.requests == ["healthy.md", "missing.md", "healthy.md", "missing.md"]
    assert client.states == {"healthy.md": "healthy", "missing.md": "healthy"}


def test_reupload_retry_after_partial_completion_is_restartable(tmp_path: Path) -> None:
    from scripts.seed_demo import upload_sources

    source_list = write_sources(tmp_path, ["first.md", "second.md", "third.md"])
    client = RestartableUploadClient(
        {"first.md": "missing", "second.md": "missing", "third.md": "missing"}, fail_once="second.md"
    )

    with pytest.raises(httpx.HTTPStatusError):
        upload_sources(client, "project-id", source_list, set(), reupload=True)
    upload_sources(client, "project-id", source_list, {"first.md"}, reupload=True)

    assert client.requests == ["first.md", "second.md", "first.md", "second.md", "third.md"]
    assert set(client.states.values()) == {"healthy"}


def test_reupload_does_not_swallow_unrelated_conflicts(tmp_path: Path) -> None:
    from scripts.seed_demo import upload_sources

    source_list = write_sources(tmp_path, ["changed.md"])
    client = RestartableUploadClient({"changed.md": "mismatch"})

    with pytest.raises(httpx.HTTPStatusError):
        upload_sources(client, "project-id", source_list, {"changed.md"}, reupload=True)
