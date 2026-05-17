"""Tests for cowork.artifacts_store (F12)."""
import pytest
from cowork.artifacts_store import ArtifactVersion, ArtifactVersionStore


@pytest.fixture()
def store():
    return ArtifactVersionStore(":memory:")


def _av(artifact_id, version=0, body="<h1>Hello</h1>", kind="html"):
    return ArtifactVersion(
        artifact_id=artifact_id, version=version,
        body=body, attrs={"kind": kind, "title": "Test"},
    )


class TestSaveAndRetrieve:
    def test_save_assigns_version_1_for_new_artifact(self, store):
        av = store.save(_av("art1"))
        assert av.version == 1

    def test_save_auto_increments_version(self, store):
        store.save(_av("art1"))
        av2 = store.save(_av("art1", body="<h1>v2</h1>"))
        assert av2.version == 2

    def test_save_explicit_version_is_honoured(self, store):
        av = store.save(_av("art1", version=5))
        assert av.version == 5

    def test_get_version_returns_correct_body(self, store):
        store.save(_av("art1", body="body-v1"))
        store.save(_av("art1", body="body-v2"))
        result = store.get_version("art1", 1)
        assert result is not None
        assert result.body == "body-v1"

    def test_get_latest_returns_highest_version(self, store):
        store.save(_av("art1", body="v1"))
        store.save(_av("art1", body="v2"))
        store.save(_av("art1", body="v3"))
        latest = store.get_latest("art1")
        assert latest.version == 3
        assert latest.body == "v3"

    def test_get_version_missing_returns_none(self, store):
        assert store.get_version("nonexistent", 1) is None

    def test_get_latest_missing_returns_none(self, store):
        assert store.get_latest("nonexistent") is None


class TestListVersions:
    def test_list_versions_ordered_asc(self, store):
        store.save(_av("a1", body="v1"))
        store.save(_av("a1", body="v2"))
        store.save(_av("a1", body="v3"))
        versions = store.list_versions("a1")
        assert [v.version for v in versions] == [1, 2, 3]

    def test_list_versions_empty_for_unknown(self, store):
        assert store.list_versions("missing") == []

    def test_list_artifacts_summary(self, store):
        store.save(_av("a1", kind="html"))
        store.save(_av("a1", kind="html"))
        store.save(_av("a2", kind="markdown"))
        summaries = store.list_artifacts()
        ids = {s["artifact_id"] for s in summaries}
        assert "a1" in ids and "a2" in ids

    def test_version_count(self, store):
        for _ in range(4):
            store.save(_av("art_x"))
        assert store.version_count("art_x") == 4

    def test_list_artifacts_kind_in_attrs(self, store):
        store.save(_av("art_k", kind="react"))
        summaries = store.list_artifacts()
        assert summaries[0]["kind"] == "react"
