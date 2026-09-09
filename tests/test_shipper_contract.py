"""Validate actual shipper output against the existing, authoritative payload schema."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("language", ["python", "node"])
def test_emitted_files_match_contract(language, tmp_path):
    if language == "python":
        sys.path.insert(0, str(ROOT / "packages/traj-shipper-py"))
        from traj_shipper import TrajCapture
        cap = TrajCapture(agent_name="contract-agent", sas_url=f"file://{tmp_path}", start_worker=False, flush_on_exit=False)
        cap.start("run-0001", {"model": "test"})
        cap.turn("run-0001", 1, {"role": "user", "content": "synthetic"})
        cap.end("run-0001", {"turns": 1})
        cap.drain()
    else:
        node = shutil.which("node")
        if not node:
            pytest.skip("Node runtime is not installed")
        script = """
const {TrajCapture} = require(process.argv[1]);
(async () => {
 const c = new TrajCapture({agentName:'contract-agent',sasUrl:'file://'+process.argv[2],flushOnExit:false});
 c.start('run-0001',{model:'test'}); c.turn('run-0001',1,{role:'user',content:'synthetic'}); c.end('run-0001',{turns:1});
 if (!(await c.flush(2000))) throw new Error('capture did not complete');
 await c.close(0);
})();
"""
        subprocess.run([node, "-e", script, str(ROOT / "packages/traj-shipper-node"), str(tmp_path)], check=True, timeout=5)
    schema = json.loads((ROOT / "spec/traj-v1.schema.json").read_text())
    run = tmp_path / "trajectories/contract-agent/default/run-0001"
    for filename, kind in [("start.json", "start"), ("turns/0001.json", "turn"), ("end.json", "end"), ("manifest.json", "manifest")]:
        jsonschema.validate(json.loads((run / filename).read_text()), {**schema, **schema["$defs"][kind]})
    receipt = json.loads((tmp_path / "trajectories/_receipts/run-0001.json").read_text())
    jsonschema.validate(receipt, {**schema, **schema["$defs"]["receipt"]})
    manifest = json.loads((run / "manifest.json").read_text())
    for filename, info in manifest["files"].items():
        data = (run / filename).read_bytes()
        assert info == {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
