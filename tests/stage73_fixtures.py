"""Synthetic canaries only. Not an owner authorization for any real-home target."""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import canary_contract as contract
import home_assistant_inventory as graph
import home_assistant_read as reader
import shadow_action_policy as policy
from canary_verifier import CanaryVerifier


def document() -> dict:
    areas = [{"area_id": f"room-{i}", "name": name} for i, name in enumerate(("Мастерская", "Кладовая", "Мансарда"))]
    devices = [{"id": f"device-{i}", "name": name, "area_id": f"room-{i}"}
               for i, name in enumerate(("Свет мастерской", "Лампа кладовой", "Ночник мансарды"))]
    entities = [{"id": f"entity-{i}", "entity_id": f"light.fixture_{i}", "name": device["name"],
                 "device_id": device["id"], "platform": "fixture"} for i, device in enumerate(devices)]
    states = [{"entity_id": e["entity_id"], "state": "off", "attributes": {"friendly_name": e["name"]}} for e in entities]
    return graph.build_inventory(entities, devices, areas, states)


def config(doc: dict, enabled: bool = True) -> contract.ControlConfig:
    areas = {area["area_ref"]: area["name"] for area in doc["area_nodes"]}
    records = [{
        "canary_id": f"fixture-{index}", "target_ref": e["entity_ref"], "entity_ref": e["entity_ref"],
        "entity_id": e["entity_id"], "physical_identity": e["target_ref"], "domain": e["domain"],
        "registry_area_ref": e["area_ref"], "registry_area": areas[e["area_ref"]],
        "owner_area": None,
        "allowed_actions": ["turn_on", "turn_off"], "verification_profile": "stable_boolean_state",
        "rollback_actions": ["turn_on", "turn_off"], "allow_noop": True,
    } for index, e in enumerate(doc["entities"])]
    return contract.ControlConfig.parse({"CONTROL_ENABLED": enabled, "CANARY_LIVE_ENABLED": enabled,
                                         "owner_approval": "fixture-only", "canaries": records})


def shadow(doc: dict, cfg: contract.ControlConfig, index: int = 0, action: str = "turn_on") -> policy.ActionPlan:
    record = cfg.records[index]
    entity = next(e for e in doc["entities"] if e["entity_ref"] == record.entity_ref)
    decision = policy.ACTION_POLICY_REGISTRY.evaluate(action, {"domains": {record.domain}})
    return policy.seal_action_plan(target_ref=record.target_ref, target_label=entity["display_name"],
                                   areas=[record.registry_area], domain=record.domain, action=action,
                                   scope=policy.ActionScope(requested_areas=(record.registry_area,)), decision=decision)


class FakeHA:
    """A real loopback HTTP server, with independently counted received POSTs."""
    def __init__(self, doc: dict) -> None:
        self.document = doc
        self.states = {e["entity_id"]: "off" for e in doc["entities"]}
        self.posts = []
        self.reads = 0
        self.mode = "success"
        self.post_read = 0
        self.latest = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, raw):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                if self.path != "/api/states":
                    return self.reply(404, b"{}")
                fixture.reads += 1
                if fixture.latest:
                    fixture.post_read += 1
                    entity, desired = fixture.latest
                    if fixture.mode == "transient" and fixture.post_read >= 2:
                        fixture.states[entity] = "off" if desired == "on" else "on"
                    if fixture.mode == "delayed" and fixture.post_read >= 2:
                        fixture.states[entity] = desired
                self.reply(200, json.dumps([{"entity_id": key, "state": value, "attributes": {}}
                                           for key, value in fixture.states.items()]).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path not in {"/api/services/light/turn_on", "/api/services/light/turn_off",
                                     "/api/services/switch/turn_on", "/api/services/switch/turn_off"}:
                    return self.reply(400, b"{}")
                if set(body) != {"entity_id"} or body["entity_id"] not in fixture.states:
                    return self.reply(400, b"{}")
                entity = body["entity_id"]
                desired = "on" if self.path.endswith("turn_on") else "off"
                fixture.posts.append((self.path, entity))
                fixture.latest = (entity, desired)
                fixture.post_read = 0
                if fixture.mode not in {"no_change", "wrong_state", "http500", "delayed", "malformed", "timeout_after_unchanged"}:
                    fixture.states[entity] = desired
                if fixture.mode == "other_changed":
                    other = next(key for key in fixture.states if key != entity)
                    fixture.states[other] = "on"
                if fixture.mode in {"timeout_after", "timeout_after_unchanged"}:
                    time.sleep(0.25)
                if fixture.mode == "reset":
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                self.reply(500 if fixture.mode == "http500" else 200,
                           b"malformed" if fixture.mode == "malformed" else b"[]")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class FixtureVerifier(CanaryVerifier):
    def __init__(self, fake: FakeHA) -> None:
        super().__init__(reader.AdapterConfig("http", "127.0.0.1", fake.server.server_port,
                                             "fixture-read-credential", (), True))
        self.fake = fake
        self.age = 0

    def metadata(self):
        return copy.deepcopy(self.fake.document), time.monotonic() - self.age
