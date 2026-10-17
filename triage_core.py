"""
Core logic for the Incident Triage Assistant (same design as incident_triage_assistant.ipynb).

The only addition is `on_event`: an optional callback that triage() calls every time
something happens (a tool call, a tool result, a validation failure), so a UI can show
the investigation live.
"""

import json
from typing import Callable, List, Literal, Optional

from anthropic import Anthropic
from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

load_dotenv(find_dotenv())  # Finds the .env file in the course folder (one level up).
client = Anthropic()

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TURNS = 8

# ---------------------------------------------------------------------------
# Fake monitoring data (stands in for a real system like Grafana/Datadog)
# ---------------------------------------------------------------------------
SERVICES = {
    "checkout-service": {
        "error_rate_pct": 7.4,
        "p95_latency_ms": 2300,
        "recent_deploys": [
            {"version": "v2.18.0", "deployed_at": "13:52", "change": "New promo-code discount logic"}
        ],
    },
    "payments-api": {
        "error_rate_pct": 0.3,
        "p95_latency_ms": 410,
        "recent_deploys": [],
    },
    "auth-service": {
        "error_rate_pct": 0.1,
        "p95_latency_ms": 120,
        "recent_deploys": [
            {"version": "v5.2.1", "deployed_at": "09:10", "change": "Dependency bump"}
        ],
    },
}


def service_health(metrics: dict) -> str:
    """Classify a service using the same thresholds as the severity rules."""
    if metrics["error_rate_pct"] > 5 or metrics["p95_latency_ms"] > 2000:
        return "major"
    if metrics["error_rate_pct"] > 1 or metrics["p95_latency_ms"] > 1000:
        return "degraded"
    return "healthy"


# ---------------------------------------------------------------------------
# Local tools
# ---------------------------------------------------------------------------
def list_services():
    """Return a sorted list of all service names."""
    return sorted(SERVICES)


def get_service_metrics(service_name):
    """Return the name, error rate, latency and recent deploys for one service."""
    if service_name not in SERVICES:
        raise ValueError(f"Unknown service '{service_name}'. Call list_services first.")
    return {"service": service_name, **SERVICES[service_name]}


LOCAL_TOOLS = {"list_services": list_services, "get_service_metrics": get_service_metrics}


# ---------------------------------------------------------------------------
# Structured, validated report
# ---------------------------------------------------------------------------
class TriageReport(BaseModel):
    severity: Literal["SEV1", "SEV2", "SEV3"] = Field(
        description="SEV1 = major customer impact, SEV2 = partial, SEV3 = minor"
    )
    summary: str = Field(max_length=300, description="One or two sentences describing the incident")
    affected_services: List[str] = Field(min_length=1, description="Services that are degraded")
    likely_cause: str = Field(description="The most likely root cause, based on the evidence")
    evidence: List[str] = Field(min_length=1, description="Concrete facts from tool results")
    next_steps: List[str] = Field(min_length=1, max_length=5, description="Ordered actions for responders")
    confidence: float = Field(ge=0, le=1, description="Confidence in likely_cause, from 0 to 1")

    @field_validator("affected_services")
    @classmethod
    def services_must_exist(cls, value):
        unknown = [s for s in value if s not in SERVICES]
        if unknown:
            raise ValueError(f"Unknown services: {unknown}. Valid: {sorted(SERVICES)}")
        return value


TOOLS = [
    {
        "name": "list_services",
        "description": "Lists all services in production. Use this before looking up metrics.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_service_metrics",
        "description": "Returns the current error rate (%), p95 latency (ms) and recent deploys for one service. "
                       "Use it to check a service's health during an incident.",
        "input_schema": {
            "type": "object",
            "properties": {"service_name": {"type": "string", "description": "Exact service name"}},
            "required": ["service_name"],
        },
    },
    {
        "name": "submit_triage_report",
        "description": "Submit the FINAL structured triage report. Call this once you have gathered evidence.",
        "input_schema": TriageReport.model_json_schema(),
    },
]

SYSTEM_PROMPT = """You are an on-call incident triage assistant for an online shop.

How to investigate:
- Always call list_services first, then get_service_metrics for every service before concluding.
- Only cite facts returned by the tools. Never invent numbers, times or version numbers.
  Only mention a version if a tool returned it (for example, do not suggest rolling back to a version you were not given).

Match the report to the evidence:
- Identify which service the user's symptom relates to:
  checkout/basket/order -> checkout-service, card/payment -> payments-api, login/sign-in -> auth-service.
- Check that service's metrics. If they are healthy, say so clearly.
- Do NOT blame a different service just because it is unhealthy, unless the symptom clearly relates to it.
- Describe each service's health accurately (do not call a broken service "normal", or a healthy one "elevated").

Severity rules:
- SEV1: many customers cannot complete a core action (checkout, payment or login) AND the metrics show a major problem
  (error rate above 5% or p95 latency above 2000ms).
- SEV2: some customers are affected AND the metrics show a real, measurable degradation
  (error rate above 1% or p95 latency above 1000ms).
- SEV3: only one or a few reports, OR the related service's metrics look healthy.
- If unsure between two levels, choose the lower one unless the metrics clearly show widespread impact.

Finishing:
- Set confidence lower when the evidence is weak or the metrics do not support the report.
- When you are done, call submit_triage_report exactly once."""

EventCallback = Optional[Callable[[str, dict], None]]


def _emit(on_event: EventCallback, kind: str, **data):
    if on_event:
        on_event(kind, data)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def chat(messages, model):
    return client.messages.create(
        model=model, max_tokens=1500, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages
    )


def run_tools(response, on_event: EventCallback = None):
    """Run every requested tool. Returns (tool_result_blocks, validated_report_or_None)."""
    results, report = [], None
    for block in response.content:
        if block.type != "tool_use":
            continue
        _emit(on_event, "tool_call", name=block.name, input=block.input)
        try:
            if block.name == "submit_triage_report":
                report = TriageReport.model_validate(block.input)
                output = "Report accepted."
                _emit(on_event, "report_accepted")
            else:
                output = LOCAL_TOOLS[block.name](**block.input)
                _emit(on_event, "tool_result", name=block.name, output=output)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(output), "is_error": False})
        except ValidationError as e:
            _emit(on_event, "validation_failed", errors=[err["msg"] for err in e.errors()])
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": f"Validation failed, fix and resubmit:\n{e}", "is_error": True})
        except Exception as e:
            _emit(on_event, "tool_error", name=block.name, error=str(e))
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": f"Error: {e}", "is_error": True})
    return results, report


def triage(incident: str, model: str = DEFAULT_MODEL, on_event: EventCallback = None) -> TriageReport:
    """Investigate an incident until Claude submits a valid report (or MAX_TURNS is reached)."""
    messages = [{"role": "user", "content": incident}]
    for turn in range(1, MAX_TURNS + 1):
        _emit(on_event, "thinking", turn=turn)
        response = chat(messages, model)
        messages.append({"role": "assistant", "content": response.content})

        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if text:
            _emit(on_event, "claude_text", text=text)

        if response.stop_reason != "tool_use":
            messages.append({"role": "user", "content": "Please submit your findings with submit_triage_report."})
            continue

        tool_results, report = run_tools(response, on_event)
        if report is not None:
            return report
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(f"No valid report after {MAX_TURNS} turns")


if __name__ == "__main__":  # Quick command-line test: python triage_core.py
    print(triage("Customers say checkout is failing with 500 errors since about 2pm.",
                 on_event=lambda k, d: print("  ", k, d)).model_dump_json(indent=2))
