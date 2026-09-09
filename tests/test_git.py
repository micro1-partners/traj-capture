from tests.conftest import _git


def test_git_info_on_repo(capture, make_repo):
    repo = make_repo()
    info = capture.git_info(repo / ".")
    assert info["root"] == str(repo)
    assert info["remote"] == "https://github.com/testco/app.git"
    assert info["branch"] == "main"
    assert len(info["head"]) == 40


def test_git_info_outside_repo_is_none(capture, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert capture.git_info(d) is None


def test_dirty_diff_includes_modified_and_untracked(capture, make_repo):
    repo = make_repo()
    (repo / "README.md").write_text("hello\nworld\n")
    (repo / "new.txt").write_text("x")
    d = capture.git_dirty_diff(repo)
    assert "+world" in d["diff"]
    assert d["untracked"] == ["new.txt"]
    assert d["truncated"] is False


def test_commits_since_marks_user_authorship(capture, make_repo):
    repo = make_repo()
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add a")
    commits = capture.git_commits_since(repo, base)
    assert len(commits) == 1
    assert commits[0]["subject"] == "add a"
    assert commits[0]["author_email"] == "test@example.com"
    assert commits[0]["author_is_user"] is True


def test_final_diff_covers_committed_and_uncommitted(capture, make_repo):
    repo = make_repo()
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add a")
    (repo / "README.md").write_text("hello\nedited\n")
    d = capture.git_final_diff(repo, base)
    assert "a.txt" in d["diff"] and "+edited" in d["diff"]


def test_cap_text(capture):
    s, t = capture.cap_text("abcdef", 3)
    assert s == "abc" and t is True
    s, t = capture.cap_text("ab", 3)
    assert s == "ab" and t is False
