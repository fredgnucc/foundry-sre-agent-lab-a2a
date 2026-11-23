"""Layer 7 - hosted agent: zip agents/<name>/ (flat, deterministic), create the agent or
post a version (an unchanged zip returns the current one), wait for the remote build,
give its identity read access to the knowledge base, then ask it one question.

The project data plane is private: everything here goes through the SOCKS tunnel."""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

from labkit import arm, foundry, gateway, knowledge, network, runtime, tunnel
from labkit.config import Config, Names

API = "v1"
SCOPE = "https://ai.azure.com/.default"
SEARCH_INDEX_DATA_READER = "1407120a-92aa-4202-b7e9-c0e197c71c8f"


def project_url(cfg: Config, n: Names) -> str:
    return f"https://{n.account}.services.ai.azure.com/api/projects/{cfg.foundry.project}"


def _url(cfg: Config, n: Names, path: str, query: str = "") -> str:
    return f"{project_url(cfg, n)}{path}?api-version={API}{query}"


def build_zip(source: Path) -> bytes:
    """Same files -> same bytes -> no new version."""
    files = sorted(p for p in source.rglob("*") if p.is_file() and not any(
        part.startswith(".") or part == "__pycache__" for part in p.relative_to(source).parts))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in files:
            info = zipfile.ZipInfo(p.relative_to(source).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # Unix regular file bits: without them the remote build finds no requirements.txt.
            info.create_system, info.external_attr = 3, 0o100644 << 16
            z.writestr(info, p.read_bytes())
    return buf.getvalue()


def _deploy(c: arm.Client, cfg: Config, n: Names) -> str:
    a = cfg.agent
    code = build_zip(cfg.root / a.source)
    sha = hashlib.sha256(code).hexdigest()
    metadata = {"description": "foundry-to-sre-agent lab.py", "definition": {
        "kind": "hosted", "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "cpu": a.cpu, "memory": a.memory,
        "code_configuration": {"runtime": "python_3_13", "entry_point": ["python", "main.py"],
                               "dependency_resolution": "remote_build"},
        "environment_variables": {
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": cfg.foundry.model.name, "AI_GATEWAY_URL": gateway.entra_url(n),
            "SEARCH_ENDPOINT": runtime.search_url(n), "SEARCH_API_VERSION": knowledge.API,
            "KNOWLEDGE_BASE": cfg.knowledge.base, "KNOWLEDGE_SOURCE": cfg.knowledge.source}}}
    files = {"metadata": ("metadata.json", json.dumps(metadata), "application/json"),
             "code": (f"{a.name}.zip", code, "application/zip")}
    print(f"  zip {len(code)} bytes, sha256 {sha[:12]}")
    exists = arm.data(c, "GET", _url(cfg, n, f"/agents/{a.name}"), SCOPE, ok=(404,), rbac_wait=900).status_code == 200
    if exists:
        r = arm.data(c, "POST", _url(cfg, n, f"/agents/{a.name}"), SCOPE, files=files,
                     headers={"x-ms-code-zip-sha256": sha}, timeout=300)
    else:
        r = arm.data(c, "POST", _url(cfg, n, "/agents"), SCOPE, files=files,
                     headers={"x-ms-code-zip-sha256": sha, "x-ms-agent-name": a.name}, timeout=300)
    body = arm.decode(r)
    return str((body.get("versions", {}).get("latest") or body)["version"])


def _wait_active(c: arm.Client, cfg: Config, n: Names, version: str, timeout: int = 1200) -> dict:
    deadline, last = time.time() + timeout, None
    while True:
        v = arm.decode(arm.data(c, "GET", _url(cfg, n, f"/agents/{cfg.agent.name}/versions/{version}"), SCOPE, quiet=True))
        status = v.get("status")
        if status != last:
            print(f"    ... {cfg.agent.name} v{version}: {status}")
            last = status
        if status == "active":
            return v
        if status == "failed":
            raise arm.ArmError(f"agent build failed: {json.dumps(v.get('error'))[:1500]}")
        if time.time() > deadline:
            raise arm.ArmError(f"agent still {status} after {timeout}s")
        time.sleep(10)


def ask(c: arm.Client, cfg: Config, n: Names, question: str, name: str | None = None) -> str:
    """One turn on an agent's Responses endpoint (hosted or prompt); prints tool calls, returns the answer."""
    r = arm.data(c, "POST", _url(cfg, n, f"/agents/{name or cfg.agent.name}/endpoint/protocols/openai/responses"), SCOPE,
                 {"input": question, "stream": False}, timeout=300)
    answer = ""
    for item in arm.decode(r).get("output", []):
        if item.get("type") == "function_call":
            print(f"  tool {item.get('name')}({item.get('arguments')})")
        elif item.get("type") == "azure_ai_search_call":
            print(f"  tool azure_ai_search({item.get('arguments', '')})")
        elif item.get("type") == "message":
            answer += "".join(p.get("text", "") for p in item.get("content", []))
    return answer


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    if c.dry_run:
        print("== agent: data plane, skipped in dry-run")
        return
    a = cfg.agent
    rg = network.network(cfg, cfg.foundry.network).resourceGroup
    tunnel.up(c, cfg, n)
    print("== my role on the project account (create and call agents)")
    arm.role(c, arm.res_id(c, rg, "Microsoft.CognitiveServices/accounts", n.account), arm.claims(c)["oid"],
             foundry.FOUNDRY_USER, "User")

    print(f"== hosted agent {a.name} from {a.source}/")
    version = _deploy(c, cfg, n)
    v = _wait_active(c, cfg, n, version)
    principal = (v.get("instance_identity") or {}).get("principal_id")
    if not principal:
        agent = arm.decode(arm.data(c, "GET", _url(cfg, n, f"/agents/{a.name}"), SCOPE))
        principal = agent["versions"]["latest"]["instance_identity"]["principal_id"]
    print(f"== agent identity {principal}: Search Index Data Reader on {n.search}")
    arm.role(c, runtime.ids(c, cfg, n)["search"], principal, SEARCH_INDEX_DATA_READER)

    q = "In one sentence: which subnet hosts the Foundry agent compute, and what is its prefix?"
    print(f"  ask: {q}\n  answer: {ask(c, cfg, n, q)}")
