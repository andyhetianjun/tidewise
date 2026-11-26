import json
import os
import re

from tidewise.site import build_site


async def test_site_builds_with_working_relative_links(tmp_path):
    await build_site(tmp_path, jobs=8)
    for page in ("index.html", "conversations.html", "eval/index.html", "eval-sloppy/index.html"):
        assert (tmp_path / page).exists()
    broken = []
    for f in tmp_path.rglob("*.html"):
        for link in re.findall(r"""(?:href|src)=['"]([^'"#]+)""", f.read_text()):
            if link.startswith("http"):
                continue
            if not os.path.exists(os.path.normpath(f.parent / link)):
                broken.append((f.name, link))
    assert broken == []
    conv = (tmp_path / "conversations.html").read_text()
    assert "not a language model" in conv and "get_hourly_weather" in conv
    # the sloppy report marks invented claims
    assert "<mark>" in (tmp_path / "eval-sloppy/cases/hilo-halifax-high-tomorrow.html").read_text()
    assert json.loads((tmp_path / "site.json").read_text())["wall_s"]["offline"] > 0


async def test_site_has_a_trace_viewer_with_both_kinds_of_trace(tmp_path):
    await build_site(tmp_path, jobs=8)
    assert (tmp_path / "traces.html").exists()
    items = json.loads((tmp_path / "traces.json").read_text())
    sources = {i["source"] for i in items}
    assert sources == {"recorded", "sample run"}
    sample = next(i for i in items if i["source"] == "sample run")
    names = [s["name"] for s in sample["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"]]
    assert "invoke_agent tidewise" in names and any(n.startswith("chat ") for n in names)
    assert "recorded" in (tmp_path / "traces.html").read_text()
