"""architect: Foundry hosted agent that answers questions about the lab's architecture.

Grounding comes from a Foundry IQ knowledge base (AI Search, private endpoint), and the
model is reached only through the AI gateway's keyless Entra API. lab.py sets:
  AI_GATEWAY_URL, AZURE_AI_MODEL_DEPLOYMENT_NAME, SEARCH_ENDPOINT, KNOWLEDGE_BASE,
  KNOWLEDGE_SOURCE, SEARCH_API_VERSION
"""

import json
import os
import socket
from urllib.parse import urlparse

import aiohttp
from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncOpenAI

GATEWAY_SCOPE = "https://ai.azure.com/.default"
SEARCH_SCOPE = "https://search.azure.com/.default"
credential = DefaultAzureCredential()


@tool(approval_mode="never_require")
async def search_architecture(question: str) -> str:
    """Search the lab's architecture knowledge base (networks, subnets, private endpoints and
    IPs, Foundry accounts, the AI gateway and its APIs, agents, traffic flows, KQL, teardown).
    Pass a focused natural-language question; returns the matching documentation chunks."""
    token = (await credential.get_token(SEARCH_SCOPE)).token
    url = (f"{os.environ['SEARCH_ENDPOINT']}/knowledgebases/{os.environ['KNOWLEDGE_BASE']}/retrieve"
           f"?api-version={os.environ.get('SEARCH_API_VERSION', '2026-04-01')}")
    body = {"intents": [{"type": "semantic", "search": question}],
            "knowledgeSourceParams": [{"knowledgeSourceName": os.environ["KNOWLEDGE_SOURCE"], "kind": "searchIndex"}]}
    async with aiohttp.ClientSession() as http:
        async with http.post(url, json=body, headers={"Authorization": "Bearer " + token}) as r:
            if r.status != 200:
                return json.dumps({"error": r.status, "detail": (await r.text())[:500]})
            data = await r.json()
    text = "".join(p.get("text", "") for m in data.get("response", []) for p in m.get("content", []))
    return text[:16000] or "no matching documents"


@tool(approval_mode="never_require")
def where_am_i() -> str:
    """Report where this agent runs: sandbox hostname, the IP the AI gateway resolves to, and the local source IP used to reach it."""
    gateway = urlparse(os.environ["AI_GATEWAY_URL"]).hostname
    facts = {"hostname": socket.gethostname(), "ai_gateway": gateway}
    try:
        facts["ai_gateway_ip"] = socket.gethostbyname(gateway)
        # A UDP "connect" only picks the route (nothing is sent) and reveals the source IP.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((facts["ai_gateway_ip"], 443))
            facts["source_ip"] = probe.getsockname()[0]
    except OSError as exc:
        facts["error"] = str(exc)
    return json.dumps(facts)


def main() -> None:
    gateway = AsyncOpenAI(base_url=os.environ["AI_GATEWAY_URL"],
                          api_key=get_bearer_token_provider(credential, GATEWAY_SCOPE))
    agent = Agent(
        client=OpenAIChatClient(model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"], async_client=gateway),
        instructions=(
            "You are the architect of a private Azure lab that connects an Azure SRE Agent to Microsoft Foundry "
            "through an AI gateway (APIM). Always call search_architecture before answering a question about the lab, "
            "and answer only from what it returns: names, subnets, IPs, APIs, flows. If the knowledge base does not "
            "cover it, say so. Use where_am_i when asked where you run. Be concise: short paragraphs or bullets, "
            "include exact resource names and addresses."),
        tools=[search_architecture, where_am_i],
        default_options={"store": False},
    )
    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    main()
