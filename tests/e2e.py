"""Live end-to-end check against a running server. Needs a real API key.

    python app.py                 # in one terminal
    python tests/e2e.py           # in another

Walks the full pipeline and prints timings, then checks the two behaviours that
are easy to regress: multilingual follow-ups (see build_recap in app.py) and
refusal to answer from outside the document.
"""

import json
import os
import sys
import time

import requests

BASE = os.environ.get("DOC_HELPER_URL", "http://localhost:8000")
ROOT = os.path.join(os.path.dirname(__file__), "..")
SAMPLE = os.path.join(ROOT, "samples", "rental-agreement.pdf")
SESSION = f"e2e-{int(time.time())}"

failures = []


def check(name, cond, detail=""):
    print(f"  {'pass' if cond else 'FAIL'}  {name}{' ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def ask(question, language):
    """Drive the SSE endpoint. Returns (answer, seconds, first_token_seconds)."""
    started = time.time()
    answer, first, event = "", None, None
    with requests.post(
        f"{BASE}/respond",
        json={"text": question, "session": SESSION, "language": language},
        stream=True,
        timeout=300,
    ) as r:
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event == "answer":
                if first is None:
                    first = time.time() - started
                answer = json.loads(line[6:])["text"]
    return answer.strip(), time.time() - started, first


def script(text):
    if any("ऀ" <= c <= "ॿ" for c in text):
        return "hindi"
    if any("ಀ" <= c <= "೿" for c in text):
        return "kannada"
    return "english"


print(f"\ntarget {BASE}")
try:
    requests.get(f"{BASE}/health", timeout=5).raise_for_status()
except Exception:
    sys.exit(f"No server at {BASE}. Start it with: python app.py")

print("\ndocument ingestion")
started = time.time()
with open(SAMPLE, "rb") as f:
    r = requests.post(
        f"{BASE}/document",
        files={"file": ("rental-agreement.pdf", f, "application/pdf")},
        data={"session": SESSION, "language": "en-IN"},
    )
check("upload accepted", r.status_code == 200)

status = {}
while time.time() - started < 300:
    status = requests.get(f"{BASE}/document/status", params={"session": SESSION}).json()
    if status["status"] in ("ready", "failed"):
        break
    time.sleep(1.5)

check(
    "Doc AI finished",
    status.get("status") == "ready",
    f"{time.time() - started:.1f}s, {status.get('chars', 0)} chars",
)
if status.get("status") != "ready":
    sys.exit("cannot continue without a document")

print("\nfield extraction")
started = time.time()
r = requests.post(
    f"{BASE}/extract",
    json={
        "session": SESSION,
        "fields": "landlord name, monthly rent, security deposit, notice period, pet policy",
    },
    timeout=300,
)
fields = r.json().get("fields", {})
check("extraction returned JSON", r.status_code == 200, f"{time.time() - started:.1f}s")
check("found the rent", "28,500" in str(fields.get("monthly rent")))
check("found the landlord", "Ramesh" in str(fields.get("landlord name")))
check(
    "absent field is null, not invented",
    fields.get("pet policy") is None,
    "-> grounding held",
)

print("\ngrounded answers")
answer, secs, first = ask("What is the monthly rent and when is it due?", "en-IN")
check("answers in English", script(answer) == "english", f"{secs:.1f}s (first {first:.1f}s)")
check("quotes the rent", "28,500" in answer)

answer, secs, _ = ask("मेरी सिक्योरिटी डिपॉज़िट कितनी है?", "hi-IN")
check("answers in Hindi", script(answer) == "hindi", f"{secs:.1f}s")
check("quotes the deposit", "1,71,000" in answer)

print("\nmultilingual follow-ups (the regression that motivated build_recap)")
answer, secs, _ = ask("And what about the notice period?", "en-IN")
check(
    "English follow-up after a Hindi turn stays English",
    script(answer) == "english",
    f"{secs:.1f}s",
)
check("resolved the follow-up", "2 month" in answer.lower())

answer, secs, _ = ask("Is it refundable?", "en-IN")
check("pronoun resolves against earlier turns", "deposit" in answer.lower(), f"{secs:.1f}s")

answer, secs, _ = ask("ನನ್ನ ಬಾಡಿಗೆ ಎಷ್ಟು?", "kn-IN")
check("switches to Kannada", script(answer) == "kannada", f"{secs:.1f}s")

print("\nrefusal")
answer, secs, _ = ask("What is the landlord's blood group?", "en-IN")
check(
    "declines what the document doesn't contain",
    "not" in answer.lower() or "does not" in answer.lower(),
    f"-> {answer[:70]}",
)

print()
if failures:
    print(f"{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("all end-to-end checks passed")
