# QA probes

Adversarial scripts used for the QA pass described in ../QA_REPORT.md. They run against throw-away data
directories (never `data/`). Run from the repo root with the backend venv:

    backend\.venv\Scripts\python.exe qa\probe1.py       # uploads, races, handoff, tools (fake model)
    backend\.venv\Scripts\python.exe qa\probe2.py       # auth, SSRF, load, big inputs, streaming (fake model)
    backend\.venv\Scripts\python.exe qa\probe2_fuzz.py  # OpenAPI-driven fuzzer
    backend\.venv\Scripts\python.exe qa\probe3_llm.py   # prompt-injection etc. against the REAL model (slow, needs Ollama)

Post-fix checks: `confirm2.py` (lockout + API latency during a 20 MB ingest), `profile_loop_lag.py` (which ingestion stage
stalls the event loop), `probe5_leave_prompt.py` (real model: does it ask for missing leave dates?).
Some lines in probe1/probe2 output are stale on purpose: they predate action confirmation (tickets and leave are no
longer created until the user confirms) and rate limiting (the load probe now sees 429s).

Output is one line per probe: [OK] held up, [BUG] confirmed defect, [NOTE] observation.
`probe4_es.py` and `confirm.py` re-check individual findings. Confirmed bugs are also encoded as strict
xfail regression tests in backend/tests/test_qa_known_bugs.py and frontend/src/test/qa.test.tsx.
