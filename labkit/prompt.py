"""Layer - prompt agent: the same architect, as a definition only. No code, no container,
no build: Foundry runs the loop itself.

  POST {project}/agents?api-version=v1          {name, definition}  first version
  POST {project}/agents/{name}?api-version=v1   {definition}        new version, only when it changed

Its model is "<account connection>/<deployment>": the connection the gateway layer created,
so Foundry calls the gateway's key API (chat/completions) from the agent subnet. The
knowledge tool is azure_ai_search on the project's search connection (Entra).

The project data plane is private: everything here goes through the SOCKS tunnel."""

from __future__ import annotations

import json

from labkit import agent, arm, tunnel
from labkit.config import Config, Names

INSTRUCTIONS = (
    "You are the architect of a private Azure lab that connects an Azure SRE Agent to Microsoft Foundry "
    "through an AI gateway (APIM). Always search the knowledge index before answering a question about the lab, "
    "and answer only from what it returns: names, subnets, IPs, APIs, flows. If it does not cover it, say so. "
    "Be concise: short paragraphs or bullets, include exact resource names and addresses.")


def definition(cfg: Config, n: Names) -> dict:
    return {"kind": "prompt", "model": f"{n.account}/{cfg.foundry.model.name}", "instructions": INSTRUCTIONS,
            "tools": [{"type": "azure_ai_search", "azure_ai_search": {"indexes": [{
                "project_connection_id": n.search, "index_name": cfg.knowledge.index,
                "query_type": "semantic", "top_k": 5}]}}]}


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    if c.dry_run:
        print("== prompt: data plane, skipped in dry-run")
        return
    name = cfg.promptAgent.name
    tunnel.up(c, cfg, n)
    want = definition(cfg, n)
    print(f"== prompt agent {name}: model {want['model']} (gateway connection), tool azure_ai_search "
          f"({n.search}/{cfg.knowledge.index})")
    r = arm.data(c, "GET", agent._url(cfg, n, f"/agents/{name}"), agent.SCOPE, ok=(404,), rbac_wait=900)
    body = {"description": "foundry-to-sre-agent lab.py", "definition": want}
    if r.status_code == 404:
        v = arm.decode(arm.data(c, "POST", agent._url(cfg, n, "/agents"), agent.SCOPE, {"name": name, **body}))
    else:
        latest = arm.decode(r)["versions"]["latest"]
        have = {k: latest["definition"].get(k) for k in want}
        if json.dumps(have, sort_keys=True) == json.dumps(want, sort_keys=True):
            print(f"  v{latest['version']} up to date")
            v = None
        else:
            v = arm.decode(arm.data(c, "POST", agent._url(cfg, n, f"/agents/{name}"), agent.SCOPE, body))
    if v:
        print(f"  v{(v.get('versions', {}).get('latest') or v)['version']} created")

    q = "In one sentence: which subnet hosts the agent compute, and what is its prefix?"
    print(f"  ask: {q}\n  answer: {agent.ask(c, cfg, n, q, name)}")
