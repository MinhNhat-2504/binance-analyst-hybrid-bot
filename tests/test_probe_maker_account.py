"""The maker probe must never reuse daily-bot credentials."""
from __future__ import annotations

import pytest

from probe_maker_account import check_not_primary, parse_probe_file


def test_probe_file_requires_explicit_isolation_and_a_distinct_key(tmp_path) -> None:
    path = tmp_path / ".env.maker_probe"
    path.write_text("PROBE_API_KEY=probe\nPROBE_API_SECRET=secret\nISOLATED_DEMO_ACCOUNT=1\n",
                    encoding="utf-8")
    assert parse_probe_file(path)["PROBE_API_KEY"] == "probe"
    check_not_primary("probe", ["daily"])
    with pytest.raises(RuntimeError, match="matches"):
        check_not_primary("probe", ["other", "probe"])
    path.write_text("PROBE_API_KEY=probe\nPROBE_API_SECRET=secret\nISOLATED_DEMO_ACCOUNT=0\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="ISOLATED_DEMO_ACCOUNT"):
        parse_probe_file(path)
