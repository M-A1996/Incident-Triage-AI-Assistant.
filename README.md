# Incident Triage Assistant

An AI on-call assistant built with the **Claude API**. Describe an incident in plain English ("checkout is failing with 500 errors since 2pm") and Claude investigates the services with tools, then returns a **validated, structured triage report** — severity, affected services, likely cause, evidence, next steps and confidence.

## How it works

1. **Tool use** — Claude calls `list_services` and `get_service_metrics` to read error rates, p95 latency and recent deploys (demo data standing in for a monitoring system).
2. **Agent loop** — a multi-turn loop runs the requested tools, sends the results back, and repeats until Claude submits its report (capped at 8 turns).
3. **Structured output** — the report is submitted through a `submit_triage_report` tool whose schema is generated from a **Pydantic** model.
4. **Repair loop** — if the report fails validation (e.g. confidence of 1.5, an unknown service), the error is sent back to Claude to fix and resubmit.
5. **Evaluation** — code-based graders check severity, affected services, and that no version numbers were invented. Improving the system prompt raised the score from **75% to 100%** on the test set.

## Project files

| File | What it is |
|---|---|
| `incident_triage_assistant.ipynb` | Step-by-step build: tools, Pydantic model, agent loop, repair loop and evaluation |
| `triage_core.py` | The assistant's logic as a reusable module |
| `app.py` | Streamlit web UI with a live investigation feed |

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # then add your own ANTHROPIC_API_KEY
streamlit run app.py
```

Open http://localhost:8501, type an incident (or pick an example) and click **Triage incident**. The sidebar shows each service's health, and the main panel streams each tool call as Claude investigates.

## Limitations

- Service metrics are hard-coded demo data, not a live monitoring integration.
- The evaluation set is small (4 cases) and checks the final report, not whether every service was investigated.
