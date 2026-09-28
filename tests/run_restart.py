"""Crash / restart scenarios for agent.py against tests/mock_server.py (SYNTHETIC mock, local only).

    ARENA_URL=http://127.0.0.1:18700 ARENA_KEY=test python tests/run_restart.py [--agent agent.py] [--only a,b]

Scenario i runs its own mock on port+i. Refuses to start unless ARENA_URL is 127.0.0.1/localhost
and ARENA_KEY is "test". Scores from the mock mean nothing; this checks behaviour only:
  full           one uninterrupted run through closed -> recon -> market -> closing -> closed
  restart_state  hard-kill just after the opening burst, restart with the state file
  restart_fresh  same, but the state file is deleted (fresh disk) before the restart
  faults         /ledger without a phase key, claimed as "false"/"true" strings, ~3% malformed JSON
"""
import argparse, json, os, re, shutil, socket, subprocess, sys, threading, time
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK = os.path.join(ROOT, "tests", "mock_server.py")
RUNS = os.path.join(ROOT, "tests", "_runs")
PHASE_ARGS = ["--wait", "3", "--recon", "40", "--market", "45", "--closing", "12"]
AGENT_ENV = {"MIN_PAGES_PER_ROLE": "20", "MAX_PAGES_PER_ROLE": "40", "LOOP_SLEEP_S": "3",
             "PYTHONUNBUFFERED": "1"}
TIMEOUT_S = 200


def assert_local(url):
    host = urlsplit(url).hostname
    if host not in ("127.0.0.1", "localhost"):
        sys.exit(f"REFUSING: ARENA_URL host {host!r} is not local")


def guard():
    url, key = os.environ.get("ARENA_URL", ""), os.environ.get("ARENA_KEY")
    assert_local(url)
    if key != "test":
        sys.exit("REFUSING: ARENA_KEY must be 'test' for local runs")
    return urlsplit(url).port or 18700


def start_mock(port, faults, run_dir):
    args = [sys.executable, MOCK, "--port", str(port)] + PHASE_ARGS + (["--faults"] if faults else [])
    out = open(os.path.join(run_dir, "mock.log"), "w")
    p = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, cwd=ROOT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    raise RuntimeError(f"mock on {port} did not start")


def start_agent(agent, port, run_dir, tag):
    env = dict(os.environ)
    env.update(AGENT_ENV, ARENA_URL=f"http://127.0.0.1:{port}", ARENA_KEY="test", PYTHONPATH=ROOT,
               STATE_PATH=os.path.join(run_dir, "agent_state.json"),
               DECISIONS_PATH=os.path.join(run_dir, f"decisions_{tag}.jsonl"))
    assert_local(env["ARENA_URL"])
    out = open(os.path.join(run_dir, f"agent_{tag}.log"), "w")
    return subprocess.Popen([sys.executable, os.path.abspath(agent)], stdout=out,
                            stderr=subprocess.STDOUT, cwd=run_dir, env=env)


def wait_for_text(path, text, timeout):
    end = time.time() + timeout
    while time.time() < end:
        with open(path, errors="replace") as f:
            if text in f.read():
                return True
        time.sleep(0.3)
    return False


def summarise(run_dir, tag, proc):
    try:
        code = proc.wait(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = "TIMEOUT"
    with open(os.path.join(run_dir, f"agent_{tag}.log"), errors="replace") as f:
        text = f.read()
    rejects, offers = {}, 0
    dpath = os.path.join(run_dir, f"decisions_{tag}.jsonl")
    if os.path.exists(dpath):
        for line in open(dpath, errors="replace"):
            d = json.loads(line)
            if d.get("action") == "offer":
                offers += 1
                if not d.get("accepted"):
                    rejects[str(d.get("reason"))] = rejects.get(str(d.get("reason")), 0) + 1
    final = re.search(r"FINAL ledger: (.*)", text)
    return {"exit": code, "ended_cleanly": bool(re.search(r"arena closed|out of credits", text)),
            "tracebacks": text.count("Traceback"), "caught_and_continued": text.count("(continuing)"),
            "signed": text.count("SIGNED "), "offers": offers, "rejects": rejects,
            "final_ledger": final.group(1) if final else None, "text": text}


def scenario(name, agent, port, results):
    run_dir = os.path.join(RUNS, name)
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir)
    mock = start_mock(port, name == "faults", run_dir)
    try:
        a = start_agent(agent, port, run_dir, "A")
        out = {}
        if name.startswith("restart"):
            if not wait_for_text(os.path.join(run_dir, "agent_A.log"), "burst done", 150):
                a.kill()
                results[name] = {"A": summarise(run_dir, "A", a), "error": "no burst before timeout"}
                return
            time.sleep(2)
            a.kill()                                                   # hard crash / redeploy
            a.wait()
            out["A"] = summarise(run_dir, "A", a)
            if name == "restart_fresh":
                os.remove(os.path.join(run_dir, "agent_state.json"))  # fresh disk
            b = start_agent(agent, port, run_dir, "B")
            out["B"] = summarise(run_dir, "B", b)
        else:
            out["A"] = summarise(run_dir, "A", a)
        results[name] = out
    finally:
        mock.kill()


def check(name, out):
    """Pass/fail rules per scenario; returns a list of failure strings."""
    fails = []
    last = out.get("B") or out.get("A")
    if "error" in out:
        return [out["error"]]
    if last["exit"] != 0:
        fails.append(f"exit={last['exit']}")
    if not last["ended_cleanly"]:
        fails.append("did not end via 'arena closed'/'out of credits'")
    if name != "faults" and last["tracebacks"]:
        fails.append(f"{last['tracebacks']} tracebacks")
    if last["tracebacks"] > last["caught_and_continued"]:
        fails.append("uncaught traceback")
    if out["A"]["signed"] == 0:
        fails.append("signed nobody")
    if name.startswith("restart"):
        b = out["B"]
        if b["rejects"].get("requisition_full"):
            fails.append(f"{b['rejects']['requisition_full']} requisition_full after restart")
        want = "restored state" if name == "restart_state" else "no state file"
        if want not in b["text"]:
            fails.append(f"restart log lacks {want!r}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=os.path.join(ROOT, "agent.py"))
    ap.add_argument("--only", default="full,restart_state,restart_fresh,faults")
    a = ap.parse_args()
    base = guard()
    names = a.only.split(",")
    results, threads = {}, []
    for i, n in enumerate(names):
        t = threading.Thread(target=scenario, args=(n, a.agent, base + i, results))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    ok = True
    print(f"\nagent={os.path.relpath(a.agent, ROOT) if a.agent.startswith(ROOT) else a.agent}  (SYNTHETIC mock)")
    for n in names:
        out = results.get(n, {"error": "scenario crashed"})
        fails = check(n, out)
        ok &= not fails
        print(f"\n[{'PASS' if not fails else 'FAIL'}] {n}" + (f": {'; '.join(fails)}" if fails else ""))
        for tag in ("A", "B"):
            if tag in out:
                s = out[tag]
                print(f"  {tag}: exit={s['exit']} clean_end={s['ended_cleanly']} signed={s['signed']} "
                      f"offers={s['offers']} rejects={s['rejects']} tracebacks={s['tracebacks']} "
                      f"caught={s['caught_and_continued']}")
                print(f"     final ledger: {s['final_ledger']}")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
