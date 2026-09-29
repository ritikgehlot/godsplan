"""SIM ONLY: run an agent script with its clock sped up SIM_SPEED x, so any agent (template, v1, m1,
final) sees compressed phases without code changes: sleep() is shorter, time()/monotonic() faster.

    SIM_SPEED=150 python sim/timewarp.py agent.py
"""
import os, runpy, sys, time

speed = float(os.environ["SIM_SPEED"])
_sleep, _time, _mono = time.sleep, time.time, time.monotonic
T0, M0 = _time(), _mono()
time.sleep = lambda s: _sleep(max(0.0, s) / speed)
time.time = lambda: T0 + (_time() - T0) * speed
time.monotonic = lambda: M0 + (_mono() - M0) * speed

agent = os.path.abspath(sys.argv[1])
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(agent))
runpy.run_path(agent, run_name="__main__")
