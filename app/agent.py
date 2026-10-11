"""The advisory agent, built with the Strands Agents SDK.

The agent gets three tools backed by the flood engine and OpenSearch, decides which
to call, and writes a two-line phone notification. Every tool call is recorded so the
UI can show how the advice was reached.

Model: a local model through Ollama by default (no account needed). Models with
native tool calling (llama3.1+, llama3.2, qwen2.5) let the agent pick its tools. Older
ones (llama3, phi3) can't take tools, so the app runs the same three tools in the
fixed order the system prompt asks for and the Strands agent writes the notification
from their outputs; the UI says which happened. Set
MODEL_PROVIDER=anthropic and ANTHROPIC_API_KEY to use Claude instead. If no model is
reachable, a deterministic template writes the advice from the same tool outputs and
the UI labels it 'template'.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import time

import httpx

PROVIDER = os.getenv("MODEL_PROVIDER", "ollama")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
TIMEOUT_S = float(os.getenv("AGENT_TIMEOUT_S", "60"))
_pool = cf.ThreadPoolExecutor(max_workers=2)

SYSTEM = """You are Paani Kahan, a monsoon commute assistant for Chennai.
You write one phone notification for a commuter about street flooding on their route.

Always call check_route first with the commuter's departure. If the usual route has
flooded streets, also call flood_history for the worst street, and best_departure.

Safe depth limits: two-wheeler 30 cm (engines stall, open drains are invisible),
car 45 cm, walking 25 cm (open drains, live wires).

Reply in exactly this format and nothing else:
HEADLINE: <at most 8 words>
ADVICE: <one sentence, at most 32 words>

Name the worst street and its forecast depth in cm. If a dry route exists, say how many
minutes it adds. Only use streets, numbers and times that the tools returned."""


def clock(now_hhmm: tuple[int, int], minutes: float) -> str:
    h, m = now_hhmm
    total = h * 60 + m + int(round(minutes))
    hh, mm = divmod(total % (24 * 60), 60)
    return f"{(hh - 1) % 12 + 1}:{mm:02d} {'AM' if hh < 12 else 'PM'}"


def model_status():
    if PROVIDER == "anthropic":
        ok = bool(os.getenv("ANTHROPIC_API_KEY"))
        return {"provider": "anthropic", "model": ANTHROPIC_MODEL, "ready": ok,
                "note": None if ok else "ANTHROPIC_API_KEY not set"}
    try:
        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=1.5)
        names = [m["name"] for m in r.json().get("models", [])]
        ok = any(n == OLLAMA_MODEL or n.startswith(OLLAMA_MODEL + ":") or n.split(":")[0] == OLLAMA_MODEL for n in names)
        return {"provider": "ollama", "model": OLLAMA_MODEL, "ready": ok,
                "note": None if ok else f"Run: ollama pull {OLLAMA_MODEL}"}
    except Exception:
        return {"provider": "ollama", "model": OLLAMA_MODEL, "ready": False, "note": f"Ollama not reachable at {OLLAMA_HOST}"}


_caps: dict[str, bool] = {}


def ollama_tools_ok() -> bool:
    """Does the Ollama model support native tool calling? (cached; asks /api/show)."""
    if OLLAMA_MODEL not in _caps:
        try:
            r = httpx.post(f"{OLLAMA_HOST}/api/show", json={"model": OLLAMA_MODEL}, timeout=3)
            caps = r.json().get("capabilities")
            _caps[OLLAMA_MODEL] = ("tools" in caps) if isinstance(caps, list) else True
        except Exception:  # noqa: BLE001
            return True
    return _caps[OLLAMA_MODEL]


WRITER = """You are Paani Kahan, a monsoon commute assistant.
You write one phone notification for a commuter about street flooding on their route,
using ONLY the tool results you are given. Do not invent streets, numbers or times.

Safe depth limits: two-wheeler 30 cm, car 45 cm, walking 25 cm.

Reply in exactly this format and nothing else:
HEADLINE: <at most 8 words>
ADVICE: <one sentence, at most 32 words>

Name the worst street and its forecast depth in cm. If a dry route exists, say how many
minutes it adds."""


def _numbers_grounded(text: str, trace) -> bool:
    """Every 'NN cm' in the model's text must appear in a tool result."""
    blob = json.dumps([t["result"] for t in trace])
    have = set(re.findall(r"\d+", blob))
    return all(n in have for n in re.findall(r"(\d+)\s*cm", text))


def _model():
    if PROVIDER == "anthropic":
        from strands.models.anthropic import AnthropicModel
        return AnthropicModel(client_args={"api_key": os.environ["ANTHROPIC_API_KEY"]},
                              model_id=ANTHROPIC_MODEL, max_tokens=400)
    from strands.models.ollama import OllamaModel
    return OllamaModel(OLLAMA_HOST, model_id=OLLAMA_MODEL, temperature=0.2)


class Advisor:
    """ctx needs: router, sim, store, persona, mode, depart_min, now (h, m)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.trace = []
        self.tools_by = None

    # ---------------------------------------------------------------- tools
    def _record(self, name, args, result, t0):
        self.trace.append({"tool": name, "args": args, "result": result,
                           "ms": round((time.perf_counter() - t0) * 1000)})

    def check_route(self, depart_in_minutes: int) -> str:
        t0 = time.perf_counter()
        c = self.ctx
        p = c["router"].plan(c["persona"]["from"], c["persona"]["to"], c["mode"], depart_in_minutes, c["sim"])
        u, d = p["usual"], p["dry"]
        res = {"departure": clock(c["now"], depart_in_minutes), "status": p["status"], "safe_limit_cm": p["limit_cm"],
               "usual_route": {"minutes": u["minutes"], "max_depth_cm": u["max_depth"],
                               "worst_street": u["worst"]["name"] if u["worst"] else None,
                               "worst_street_reached_at": clock(c["now"], u["worst"]["t"]) if u["worst"] else None,
                               "flooded_streets": [f"{f['name']} ({f['depth']} cm)" for f in u["flooded"]]},
               "dry_route": {"minutes": d["minutes"], "max_depth_cm": d["max_depth"],
                             "extra_minutes": d["minutes"] - u["minutes"]} if d else None,
               "closures_avoided": [f"{a['name']} ({a['why']})" for a in p.get("avoided", [])],
               "subways_likely_closed": p.get("subways_closed", [])}
        self._record("check_route", {"depart_in_minutes": depart_in_minutes}, res, t0)
        return json.dumps(res)

    def flood_history(self, street: str) -> str:
        t0 = time.perf_counter()
        rows = self.ctx["store"].search_reports(street, size=3)
        res = [{"date": r["date"], "report": r["text"], "depth_cm": r["depth_cm_reported"]} for r in rows]
        self._record("flood_history", {"street": street}, res, t0)
        return json.dumps(res)

    def best_departure(self) -> str:
        t0 = time.perf_counter()
        c = self.ctx
        rows = c["router"].best_departure(c["router"].nearest(*c["persona"]["from"]), c["router"].nearest(*c["persona"]["to"]),
                                          c["mode"], c["sim"], 0, 300)
        safe = [r for r in rows if r["passable"]]
        last_safe_before = None
        for r in rows:
            if r["depart"] > c["depart_min"]:
                break
            if r["passable"]:
                last_safe_before = r["depart"]
        first_safe_after = next((r["depart"] for r in rows if r["passable"] and r["depart"] > c["depart_min"]), None)
        res = {"leave_by": clock(c["now"], last_safe_before) if last_safe_before is not None else None,
               "or_wait_until": clock(c["now"], first_safe_after) if first_safe_after is not None else None,
               "safe_departures_checked": len(safe), "window": "next 5 hours"}
        self._record("best_departure", {}, res, t0)
        return json.dumps(res)

    # ---------------------------------------------------------------- run
    def _run_strands(self):
        from strands import Agent, tool

        adv = self

        @tool
        def check_route(depart_in_minutes: int) -> str:
            """Forecast flooding on the commuter's usual route and find a dry alternative.

            Args:
                depart_in_minutes: Minutes from now until the commuter leaves.
            """
            return adv.check_route(depart_in_minutes)

        @tool
        def flood_history(street: str) -> str:
            """Search past resident flood reports for a street (OpenSearch full-text search).

            Args:
                street: Street name exactly as returned by check_route.
            """
            return adv.flood_history(street)

        @tool
        def best_departure() -> str:
            """Find the latest safe time to leave before flooding, or when it clears after."""
            return adv.best_departure()

        c = self.ctx
        who = "The commuter" if c["persona"]["name"] == "You" else c["persona"]["name"]
        prompt = (f"{who} rides a {c['mode'].replace('_', '-')} from {c['persona']['from_label']} "
                  f"to {c['persona']['to_label']}. It is {clock(c['now'], 0)} now. Departure in "
                  f"{c['depart_min']} minutes ({clock(c['now'], c['depart_min'])}). Write the notification.")
        if PROVIDER == "ollama" and not ollama_tools_ok():
            # The model can't take tools: run them in the order SYSTEM prescribes, then let the agent write.
            r = json.loads(self.check_route(c["depart_min"]))
            if r["usual_route"]["flooded_streets"] and r["usual_route"]["worst_street"]:
                self.flood_history(r["usual_route"]["worst_street"])
                self.best_departure()
            results = "\n".join(f"{t['tool']}({json.dumps(t['args'])}) -> {json.dumps(t['result'])}" for t in self.trace)
            agent = Agent(model=_model(), system_prompt=WRITER, callback_handler=None)
            self.tools_by = "app"
            return str(agent(f"{prompt}\n\nTool results:\n{results}"))
        agent = Agent(model=_model(), tools=[check_route, flood_history, best_departure],
                      system_prompt=SYSTEM, callback_handler=None)
        self.tools_by = "agent"
        return str(agent(prompt))

    def template(self):
        c = self.ctx
        if not any(t["tool"] == "check_route" for t in self.trace):
            self.check_route(c["depart_min"])
        r = next(t["result"] for t in self.trace if t["tool"] == "check_route")
        u, d = r["usual_route"], r["dry_route"]
        if r["status"] == "alert":
            if not any(t["tool"] == "best_departure" for t in self.trace):
                self.best_departure()
            b = next(t["result"] for t in self.trace if t["tool"] == "best_departure")
            head = f"{u['worst_street']}: {u['max_depth_cm']} cm at {u['worst_street_reached_at']}"
            if d:
                extra = d["extra_minutes"]
                alt = f"Take the dry route ({'+' + str(extra) + ' min' if extra > 1 else 'about the same time'})"
            else:
                alt = "No dry route right now. Wait it out"
            if b.get("leave_by"):
                alt += f" or leave by {b['leave_by']}"
            adv = f"That is above the {r['safe_limit_cm']} cm limit for your {c['mode'].replace('_', '-')}. {alt}."
        elif r["status"] == "watch":
            head = f"Water building on {u['worst_street']}"
            adv = f"Forecast {u['max_depth_cm']} cm around {u['worst_street_reached_at']}. Passable for now; we'll tell you if it gets worse."
        else:
            head, adv = "Your route is dry", "No flooding expected on your way. Leave as usual."
        return head, adv

    def run(self):
        t0 = time.perf_counter()
        st = model_status()
        engine = "template"
        head = adv = None
        if st["ready"]:
            try:
                text = _pool.submit(self._run_strands).result(timeout=TIMEOUT_S)
                m1 = re.search(r"HEADLINE:\s*(.+)", text)
                m2 = re.search(r"ADVICE:\s*(.+)", text, re.S)
                if m1 and m2 and _numbers_grounded(text, self.trace):
                    head, adv = m1.group(1).strip().strip('"'), " ".join(m2.group(1).split()).strip('"')
                    engine = f"strands · {st['provider']} · {st['model']}"
                    if self.tools_by == "app":
                        engine += " · tools run by app"
                elif m1 and m2:
                    st["note"] = "Agent used a number no tool returned; showing the template instead"
            except Exception as e:  # noqa: BLE001
                st["note"] = f"Agent failed: {type(e).__name__}: {e}"[:200]
        if head is None:
            head, adv = self.template()
        return {"headline": head, "advice": adv, "engine": engine, "model": st, "tools_by": self.tools_by,
                "trace": self.trace, "ms": round((time.perf_counter() - t0) * 1000)}
