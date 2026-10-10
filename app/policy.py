"""Authorization with Cedar (cedarpy, the Python bindings for AWS's Cedar engine)."""
from __future__ import annotations

import re
import time
from collections import deque
from pathlib import Path

import cedarpy

POLICY_FILE = Path(__file__).parent / "policies" / "paani.cedar"
DECISION_LOG: deque = deque(maxlen=40)

# Demo users. In production these come from your identity provider.
USERS = {
    "priya": {"label": "Priya", "role": "resident", "desc": "Resident · two-wheeler commuter"},
    "ravi": {"label": "Ravi", "role": "ward_officer", "desc": "Ward officer"},
    "control": {"label": "Control room", "role": "emergency", "desc": "Emergency control"},
}


class Policy:
    def __init__(self, localities=()):
        self.text = POLICY_FILE.read_text()
        self.ids = re.findall(r'@id\("([^"]+)"\)', self.text)
        self.refresh(list(localities))

    def refresh(self, localities, ravi_locality=None):
        """Localities come from the tiles around you, so the entity set follows the map."""
        self.localities = list(dict.fromkeys(localities)) or ["Area"]
        loc = ravi_locality or USERS["ravi"].get("locality")
        if loc not in self.localities:
            loc = self.localities[min(1, len(self.localities) - 1)]
        USERS["ravi"]["locality"] = loc
        USERS["ravi"]["desc"] = f"Ward officer · {loc}"
        self.entities = self._entities()

    def _entities(self):
        ents = [{"uid": {"type": "Role", "id": r}, "attrs": {}, "parents": []}
                for r in ("resident", "ward_officer", "emergency")]
        for uid, u in USERS.items():
            attrs = {"suspended": False}
            if "locality" in u:
                attrs["locality"] = u["locality"]
            ents.append({"uid": {"type": "User", "id": uid}, "attrs": attrs,
                         "parents": [{"type": "Role", "id": u["role"]}]})
        for L in self.localities:
            ents.append({"uid": {"type": "Locality", "id": L}, "attrs": {"name": L}, "parents": []})
        for uid in USERS:
            ents.append({"uid": {"type": "Commute", "id": uid},
                         "attrs": {"owner": {"__entity": {"type": "User", "id": uid}}}, "parents": []})
        for layer in ("routes", "vulnerable_sites", "reports"):
            ents.append({"uid": {"type": "Layer", "id": layer}, "attrs": {}, "parents": []})
        return ents

    def check(self, user: str, action: str, rtype: str, rid: str):
        t = time.perf_counter()
        req = {"principal": f'User::"{user}"', "action": f'Action::"{action}"',
               "resource": f'{rtype}::"{rid}"', "context": {}}
        r = cedarpy.is_authorized(req, self.text, self.entities)
        reasons = [self.ids[int(p.replace("policy", ""))] if p.startswith("policy") and int(p[6:]) < len(self.ids) else p
                   for p in r.diagnostics.reasons]
        out = {"allowed": bool(r.allowed), "principal": req["principal"], "action": req["action"],
               "resource": req["resource"], "policies": reasons, "errors": list(r.diagnostics.errors),
               "ms": round((time.perf_counter() - t) * 1000, 2), "at": time.strftime("%H:%M:%S")}
        if not out["allowed"] and not reasons:
            out["note"] = "No policy permits this request (Cedar denies by default)"
        DECISION_LOG.appendleft(out)
        return out
