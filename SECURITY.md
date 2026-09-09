# Security

This code runs on machines belonging to companies in micro1's Corpus Data Program,
so we take reports seriously. Please email **security@micro1.ai** rather than opening
a public issue. Include the version (see `plugins/traj-capture/.claude-plugin/plugin.json`)
and steps to reproduce. We aim to acknowledge within two business days.

What this code holds and does not hold:

* No credentials ship in this repository. The only secret is the per-company
  enrollment code you receive from micro1, and the upload credential it is exchanged
  for, both of which live only in the local config on the enrolled machine.
* The upload credential is create+write on a dedicated container. It cannot read,
  list, or delete anything.
* Uploads go straight from the machine to Azure Blob Storage. No micro1 service sits
  in the agent's request path.
