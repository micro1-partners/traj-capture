import urllib.error
import urllib.request


def test_split_sas_url():
    from tests.conftest import load_capture
    c = load_capture()
    base, q = c.split_sas_url("https://acct.blob.core.windows.net/cont?sv=1&sig=abc")
    assert base == "https://acct.blob.core.windows.net/cont"
    assert q == "sv=1&sig=abc"


def test_dir_sink_writes_nested_file(capture, env):
    sink = capture.make_sink(f"file://{env['sink']}")
    sink.put("trajectories/claude_code/u/s/start.json", b'{"a":1}')
    assert (env["sink"] / "trajectories/claude_code/u/s/start.json").read_bytes() == b'{"a":1}'


def test_blob_sink_puts_with_headers_and_retries(capture, monkeypatch):
    calls = []

    class Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    def fake_urlopen(req, timeout=0):
        calls.append(req)
        if len(calls) < 3:
            raise urllib.error.URLError("transient")
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    sink = capture.make_sink("https://acct.blob.core.windows.net/cont?sv=1&sig=x")
    sink.put("trajectories/_probe/h.json", b"{}", content_type="application/json")
    assert len(calls) == 3
    req = calls[-1]
    assert req.get_method() == "PUT"
    assert req.full_url == "https://acct.blob.core.windows.net/cont/trajectories/_probe/h.json?sv=1&sig=x"
    assert req.get_header("X-ms-blob-type") == "BlockBlob"
    assert req.get_header("Content-type") == "application/json"


def test_blob_sink_gives_up_after_retries(capture, monkeypatch):
    def always_fail(req, timeout=0):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(urllib.request, "urlopen", always_fail)
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    sink = capture.make_sink("https://acct.blob.core.windows.net/cont?sv=1")
    try:
        sink.put("x", b"y")
        assert False, "expected failure"
    except urllib.error.URLError:
        pass
