# Trajectory payload spec (v1)

What a production agent lands, per run, under

    <slug>-traj/trajectories/<agent_name>/<instance>/<run_id>/
        start.json            once, at run start
        turns/0001.json …     one blob per turn, as it happens
        end.json              once, at run end
        feedback.json         whenever an expert scores the run (may arrive days later)
        manifest.json         written by the shipper at finalize: file hashes and counts
    <slug>-traj/trajectories/_receipts/<run_id>.json

Every file validates against the matching entry in `traj-v1.schema.json` (`$defs.start`,
`$defs.turn`, …). `example/` is a complete two-turn run.

Use globally unique, URL-safe run IDs and contiguous 1-based turn numbers. Include the
expected total in `end.turns` to detect missing tail events. A run ending and its capture
completing are separate: manifests and receipts are issued only once required events
and their upload acknowledgements reconcile. Late feedback is shipped independently.
Enable durable spooling for restart recovery. No payload schema change is required.

The one field companies have to add that they usually don't log already is the raw
provider exchange on each turn: `raw_request` (what was sent to the model) and
`raw_response` (what came back). That is what makes a trajectory replayable. The system
prompt, tool definitions, and model settings ride inside `raw_request`, so there is no
separate "agent config" file.

Validate locally:

    pip install jsonschema
    python3 -c "import json,jsonschema,sys; s=json.load(open('spec/traj-v1.schema.json')); \
      jsonschema.validate(json.load(open(sys.argv[1])), {**s, **s['\$defs'][sys.argv[2]]})" example/turns/0002.json turn
