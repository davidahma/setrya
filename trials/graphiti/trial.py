"""Exercise native episode deletion in a disposable Graphiti + Neo4j database."""
import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import distribution
import json
import os
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import parse_qs
from uuid import uuid4

TARGET = "Laptop AX17 was quarantined."
CONTROL = "Credential BR42 was reset."
NAME = "Synthetic investigation"
VERSIONS = {"0.31.1": "5961293865a57844fe13d4eb061deb3501971fb2",
            "0.31.2": "d72407dcedac911e1be9fc15b55e8c05a3efcc36"}


QUESTIONS = ("Which laptop was quarantined?", "Which credential was reset?")
CALLERS = ("incident-analyst", "shift-lead")


async def exercise(port, output):
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    import httpx
    from setrya.erasure import Subject
    from setrya.erasure.asyncio import arun_shared
    from setrya.erasure.v3 import Answer, Artifact, SharedCase, Stored
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.edges import EpisodicEdge
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client.client import LLMClient
    from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

    class Summarizer(LLMClient):
        """Deterministically summarize only the synthetic facts the upstream prompt supplies."""
        async def _generate_response(self, messages, *args, **kwargs):
            supplied = " ".join(msg.content for msg in messages)
            summary = " ".join(fact for fact in (TARGET, CONTROL) if fact in supplied)
            return {"summaries": [{"name": NAME, "summary": summary}]}

    class Embedder(EmbedderClient):
        async def create(self, *args, **kwargs):
            raise AssertionError("unexpected embedding call")

    class Ranker(CrossEncoderClient):
        async def rank(self, *args, **kwargs):
            raise AssertionError("unexpected ranking call")

    installed = distribution("graphiti-core")
    release = installed.version
    provenance = json.loads(installed.read_text("direct_url.json") or "{}")
    sha = provenance.get("vcs_info", {}).get("commit_id")
    if release not in VERSIONS or sha != VERSIONS[release]:
        raise ValueError("install the pinned Graphiti Git revision before recording evidence")
    graph = Graphiti(f"bolt://127.0.0.1:{port}", "neo4j", "", llm_client=Summarizer(None),
                     embedder=Embedder(), cross_encoder=Ranker())
    group = "trial_" + uuid4().hex
    node = EntityNode(name=NAME, group_id=group, summary=f"{TARGET} {CONTROL}", name_embedding=[1.0, 0.0])
    episodes = []
    try:
        await node.save(graph.driver)
        for who, fact in (("target", TARGET), ("control", CONTROL)):
            episode = EpisodicNode(name=who, group_id=group, source=EpisodeType.text, source_description="fixture",
                                   valid_at=datetime.now(timezone.utc), content=fact)
            await episode.save(graph.driver)
            await EpisodicEdge(source_node_uuid=episode.uuid, target_node_uuid=node.uuid, group_id=group,
                               created_at=datetime.now(timezone.utc)).save(graph.driver)
            episodes.append(episode)

        async def observe():
            saved = await EntityNode.get_by_uuid(graph.driver, node.uuid)
            sources = await EpisodicNode.get_by_entity_node_uuid(graph.driver, node.uuid)
            return {"shared_node": saved.uuid, "target_fact": TARGET in saved.summary,
                    "control_fact": CONTROL in saved.summary, "target_episode": episodes[0].uuid in {x.uuid for x in sources},
                    "control_episode": episodes[1].uuid in {x.uuid for x in sources}}

        route = f"/investigations/{node.uuid}/answer"
        tokens = {name: token_urlsafe(20) for name in CALLERS}

        async def endpoint(scope, receive, send):
            auth = dict(scope["headers"]).get(b"authorization")
            principal = next((name for name, token in tokens.items() if auth == f"Bearer {token}".encode()), None)
            question = parse_qs(scope["query_string"].decode()).get("q", [None])[0]
            status, result = 403, {}
            if scope["path"] == route and principal and question in QUESTIONS:
                # This trial-owned ASGI read path uses the persisted upstream node on every request.
                shared = await EntityNode.get_by_uuid(graph.driver, node.uuid)
                fact = TARGET if question == QUESTIONS[0] else CONTROL
                status = 200
                result = {"caller": principal, "conversation": shared.uuid, "route": scope["path"],
                          "question": question, "answer": fact if fact in shared.summary else "unknown"}
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": json.dumps(result).encode()})

        class Adopter:
            async def read(self, subject, caller, conversation, question):
                response = await client.get(f"/investigations/{conversation}/answer", params={"q": question},
                                            headers={"Authorization": "Bearer " + tokens[caller]})
                response.raise_for_status()
                row = response.json()
                return Answer(row["caller"], row["conversation"], row["route"], row["question"],
                              TARGET in row["answer"], CONTROL in row["answer"])

            async def inspect(self, subject, store, key):
                try:
                    if store == "episodes":
                        text = (await EpisodicNode.get_by_uuid(graph.driver, key)).content
                    else:
                        text = (await EntityNode.get_by_uuid(graph.driver, key)).summary
                except NodeNotFoundError:
                    return Stored(key, False, False, False, None)
                return Stored(key, True, TARGET in text, CONTROL in text, sha256(text.encode()).hexdigest())

            async def lineage(self, subject):
                related = await EpisodicNode.get_by_entity_node_uuid(graph.driver, node.uuid)
                refs = [{"store": "episodes", "key": episode.uuid, "revision": sha256(episode.content.encode()).hexdigest()}
                        for episode in related]
                shared = await self.inspect(subject, "nodes", node.uuid)
                return {"roots": [r for r in refs if r["key"] == episodes[0].uuid],
                        "derivations": [{"artifact": {"store": "nodes", "key": node.uuid, "revision": shared.revision},
                                         "inputs": refs}]}

            async def erase(self, subject):
                await graph.remove_episode(episodes[0].uuid)

        subject = Subject.derive(os.urandom(32), group, "analyst-a", "synthetic")
        case = SharedCase(node.uuid, route, CALLERS, *QUESTIONS,
                          (Artifact("episodes", episodes[0].uuid, "delete"),
                           Artifact("episodes", episodes[1].uuid, "preserve"), Artifact("nodes", node.uuid, "rewrite")),
                          require_lineage=True)
        before = await observe()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=endpoint), base_url="http://trial.local") as client:
            receipt = await arun_shared(subject, case=case, application=Adopter(), output=output,
                                        mode="deletion", run_id=f"graphiti-{sha}-{node.uuid}")
        return {"graphiti_version": release, "source_revision": sha, "before": before, "after": await observe(),
                "setrya_verdict": receipt["technical_verdict"], "receipt": output.name}
    finally:
        await graph.close()


def verdict(packet):
    version_id = packet["graphiti_version"]
    before, after = packet["before"], packet["after"]
    flags = ("target_fact", "control_fact", "target_episode", "control_episode")
    if (packet["source_revision"] != VERSIONS[version_id] or not before["shared_node"]
            or before["shared_node"] != after["shared_node"] or not all(before[f] is True for f in flags)
            or any(type(after[f]) is not bool for f in flags)):
        return "NOT TESTED"
    if after["target_episode"] or after["target_fact"] or not after["control_episode"] or not after["control_fact"]:
        return "FAIL"
    return "PASS"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", type=int, help="exercise Graphiti using loopback Neo4j and save new observations")
    parser.add_argument("--expect", choices=("PASS", "FAIL"), help="exit successfully only for this observed verdict")
    args = parser.parse_args()
    if args.port:
        packet = asyncio.run(exercise(args.port, args.output.with_suffix(".receipt")))
        with args.output.open("x") as f:
            json.dump(packet, f, sort_keys=True, indent=2)
            f.write("\n")
    packet = json.loads(args.output.read_bytes())
    decision = verdict(packet)
    if packet.get("setrya_verdict") != decision:
        raise ValueError("Native observations disagree with the Setrya verdict")
    print(f"Graphiti {packet['graphiti_version']} {decision}  {packet['after']}")
    if args.expect:
        raise SystemExit(0 if decision == args.expect else 2)
    raise SystemExit({"FAIL": 1, "PASS": 0, "NOT TESTED": 3}[decision])
