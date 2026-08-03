"""승인된 색상 동의어 런타임 사전·캐시 테스트 (이슈 #258)."""

from __future__ import annotations

from app.pipelines import color_synonyms


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Result(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Pool:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def connection(self):
        return _Conn(self.rows, self.calls)


def test_load_synonym_map_uses_approved_non_null_rows_and_deterministic_order(monkeypatch) -> None:
    calls = []
    # 쿼리 자체가 pending/null 을 제외하고, 반환 묶음은 doc_count desc → term asc.
    rows = [("남색", "네이비", 8), ("네이비", "네이비", 631), ("NAVY", "네이비", 8)]
    monkeypatch.setattr(color_synonyms, "_get_pool", lambda dsn: _Pool(rows, calls))
    mapping = color_synonyms.load_synonym_map("postgresql://x")
    assert mapping["남색"] == ["네이비", "NAVY", "남색"]
    assert mapping["navy"] == ["네이비", "NAVY", "남색"]
    sql = calls[0][0]
    assert "status = 'approved'" in sql
    assert "canonical IS NOT NULL" in sql


def test_expand_color_normalizes_lookup_but_preserves_catalog_spelling() -> None:
    mapping = {
        "남색": ["네이비", "남색"],
        "navy": ["네이비", "남색"],
        "화이트+네이비": ["화이트+네이비", "화이트네이비"],
    }
    assert color_synonyms.expand_color("  남색 ", mapping) == ["네이비", "남색"]
    assert color_synonyms.expand_color("NAVY", mapping) == ["네이비", "남색"]
    assert color_synonyms.expand_color("청록", mapping) == ["청록"]
    assert color_synonyms.expand_color("실버+블랙", mapping) == ["실버+블랙"]
    assert color_synonyms.expand_color("화이트+네이비", mapping) == [
        "화이트+네이비",
        "화이트네이비",
    ]
    assert color_synonyms.expand_color("  ", mapping) == ["  "]


def test_cache_uses_monotonic_ttl_and_reset(monkeypatch) -> None:
    color_synonyms.reset_cache()
    now = [100.0]
    loads: list[str] = []
    monkeypatch.setattr(color_synonyms.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        color_synonyms,
        "load_synonym_map",
        lambda dsn: loads.append(dsn) or {"남색": ["네이비", "남색"]},
    )

    assert color_synonyms.get_synonym_map("dsn", ttl_s=10.0)["남색"]
    now[0] = 109.9
    color_synonyms.get_synonym_map("dsn", ttl_s=10.0)
    assert loads == ["dsn"]
    now[0] = 110.0
    color_synonyms.get_synonym_map("dsn", ttl_s=10.0)
    assert loads == ["dsn", "dsn"]
    color_synonyms.reset_cache()
    color_synonyms.get_synonym_map("dsn", ttl_s=10.0)
    assert loads == ["dsn", "dsn", "dsn"]
