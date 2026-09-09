def test_shadow_init_and_diff(capture, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "brief.md").write_text("draft\n")
    (cwd / "node_modules").mkdir()
    (cwd / "node_modules" / "junk.js").write_text("x")
    big = cwd / "big.bin"
    big.write_bytes(b"\0" * (capture.SHADOW_FILE_CAP + 1))
    shadow = tmp_path / "shadow.git"

    start = capture.shadow_init(cwd, shadow)
    assert len(start["base"]) == 40
    paths = [f["path"] for f in start["manifest"]["files"]]
    assert "brief.md" in paths
    assert "node_modules/junk.js" not in paths
    assert "big.bin" not in paths
    assert start["partial"] is False
    assert not (cwd / ".git").exists()

    (cwd / "brief.md").write_text("draft\nfinal\n")
    (cwd / "report.md").write_text("new\n")
    end = capture.shadow_diff(cwd, shadow, start["base"])
    assert "+final" in end["diff"]
    assert "report.md" in end["diff"]
    assert "report.md" in [f["path"] for f in end["manifest"]["files"]]
