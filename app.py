"""
Incident Triage Assistant — web UI
Run from the practice folder:   streamlit run app.py
"""

import json
from datetime import datetime

import streamlit as st

import triage_core as core

st.set_page_config(page_title="Incident Triage Assistant", page_icon="🚨", layout="wide")

SEVERITY_STYLE = {
    "SEV1": ("🔴", "#d93025", "Major impact — act now"),
    "SEV2": ("🟠", "#e8710a", "Partial impact — investigate urgently"),
    "SEV3": ("🟢", "#188038", "Minor / no measurable impact — monitor"),
}
HEALTH_STYLE = {"major": ("🔴", "Major problem"), "degraded": ("🟠", "Degraded"), "healthy": ("🟢", "Healthy")}
EXAMPLES = [
    "Customers say checkout is failing with 500 errors since about 2pm.",
    "The checkout page has become very slow for everyone this afternoon.",
    "One customer said their card payment was declined once, but it worked on retry.",
    "A couple of users mentioned login felt slightly slow this morning.",
]

if "history" not in st.session_state:
    st.session_state.history = []          # past triages: (time, incident, report)
if "incident" not in st.session_state:
    st.session_state.incident = ""

# ---------------------------------------------------------------------------
# Sidebar: live service health + settings
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("📊 Service health")
    st.caption("Demo data the assistant's tools read from")
    for name, m in core.SERVICES.items():
        icon, label = HEALTH_STYLE[core.service_health(m)]
        with st.container(border=True):
            st.markdown(f"**{icon} {name}** · {label}")
            c1, c2 = st.columns(2)
            c1.metric("Error rate", f"{m['error_rate_pct']}%")
            c2.metric("p95 latency", f"{m['p95_latency_ms']} ms")
            if m["recent_deploys"]:
                for d in m["recent_deploys"]:
                    st.caption(f"🚀 {d['version']} at {d['deployed_at']} — {d['change']}")
            else:
                st.caption("No recent deploys")

    st.divider()
    st.header("⚙️ Settings")
    model = st.selectbox("Model", ["claude-haiku-4-5", "claude-sonnet-4-5"],
                         help="Haiku is faster and cheaper; Sonnet is stronger.")
    show_trace = st.toggle("Show investigation steps", value=True)

# ---------------------------------------------------------------------------
# Main: incident input
# ---------------------------------------------------------------------------
st.title("🚨 Incident Triage Assistant")
st.write("Describe what's going wrong in plain English. Claude investigates the services with its tools "
         "and returns a **validated triage report**.")

st.caption("Try an example:")
cols = st.columns(len(EXAMPLES))
for col, ex in zip(cols, EXAMPLES):
    if col.button(ex[:42] + "…", use_container_width=True, help=ex):
        st.session_state.incident = ex

incident = st.text_area("What's happening?", key="incident", height=100,
                        placeholder="e.g. Customers can't log in since 3pm")
run = st.button("🔍 Triage incident", type="primary", disabled=not incident.strip())


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def render_report(report: core.TriageReport):
    icon, colour, meaning = SEVERITY_STYLE[report.severity]
    st.markdown(
        f"<div style='padding:14px 18px;border-radius:10px;background:{colour};color:white;'>"
        f"<span style='font-size:1.6rem;font-weight:700'>{icon} {report.severity}</span>"
        f"<span style='margin-left:12px;font-size:1rem'>{meaning}</span></div>",
        unsafe_allow_html=True,
    )
    st.write("")
    st.subheader("Summary")
    st.write(report.summary)

    left, right = st.columns([2, 1])
    with left:
        st.markdown("**Affected services**")
        st.markdown(" ".join(f"`{s}`" for s in report.affected_services))
        st.markdown("**Likely cause**")
        st.info(report.likely_cause)
    with right:
        st.markdown("**Confidence**")
        st.progress(report.confidence, text=f"{report.confidence:.0%}")

    e_col, n_col = st.columns(2)
    with e_col:
        st.markdown("**🔎 Evidence**")
        for item in report.evidence:
            st.markdown(f"- {item}")
    with n_col:
        st.markdown("**✅ Next steps**")
        for i, step in enumerate(report.next_steps, 1):
            st.checkbox(f"{i}. {step}", key=f"step-{id(report)}-{i}")

    data = report.model_dump_json(indent=2)
    with st.expander("Raw JSON report"):
        st.code(data, language="json")
    st.download_button("⬇️ Download report (JSON)", data, file_name=f"triage-{report.severity}.json",
                       mime="application/json")


# ---------------------------------------------------------------------------
# Run the triage with a live investigation feed
# ---------------------------------------------------------------------------
if run:
    status = st.status("Claude is investigating…", expanded=show_trace)

    def on_event(kind, data):
        if kind == "thinking":
            status.update(label=f"Claude is investigating… (step {data['turn']})")
        elif kind == "claude_text" and show_trace:
            status.markdown(f"💬 _{data['text']}_")
        elif kind == "tool_call" and data["name"] != "submit_triage_report":
            args = ", ".join(f"{k}={v!r}" for k, v in data["input"].items())
            status.markdown(f"🛠️ Called **{data['name']}**({args})")
        elif kind == "tool_result" and show_trace:
            out = data["output"]
            if isinstance(out, dict):
                status.caption(f"↳ error rate {out['error_rate_pct']}%, p95 {out['p95_latency_ms']} ms, "
                               f"{len(out['recent_deploys'])} recent deploy(s)")
            else:
                status.caption(f"↳ {out}")
        elif kind == "tool_call":
            status.markdown("📝 Submitting the triage report…")
        elif kind == "validation_failed":
            status.warning("Report failed validation — asking Claude to fix it: " + "; ".join(data["errors"]))
        elif kind == "report_accepted":
            status.markdown("✅ Report passed validation")
        elif kind == "tool_error":
            status.warning(f"Tool error in {data['name']}: {data['error']}")

    try:
        report = core.triage(incident.strip(), model=model, on_event=on_event)
        status.update(label="Investigation complete", state="complete", expanded=False)
        st.session_state.history.insert(0, (datetime.now().strftime("%H:%M:%S"), incident.strip(), report))
        render_report(report)
    except Exception as e:
        status.update(label="Investigation failed", state="error")
        st.error(f"Something went wrong: {e}")
        if "api_key" in str(e).lower() or "authentication" in str(e).lower():
            st.caption("Check that ANTHROPIC_API_KEY is set in the .env file in the course folder.")

# ---------------------------------------------------------------------------
# History of this session
# ---------------------------------------------------------------------------
if st.session_state.history:
    st.divider()
    st.subheader("🕘 Recent triages (this session)")
    for when, text, rep in st.session_state.history[:10]:
        icon = SEVERITY_STYLE[rep.severity][0]
        with st.expander(f"{when} · {icon} {rep.severity} · {text[:80]}"):
            st.write(rep.summary)
            st.caption("Affected: " + ", ".join(rep.affected_services) + f" · confidence {rep.confidence:.0%}")
