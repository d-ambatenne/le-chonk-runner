"""QA: an agent checks a release's APK on an Android emulator against its issue's acceptance criteria.

    le-chonk qa run --repo https://github.com/<you>/le-chonk-game --issue 1 --release <tag>

device.py     the emulator or phone over adb: the screen's UI tree, screenshots, taps, logs
emulator.py   the dedicated emulator lechonk-qa: headless, on its own port, booted from a clean snapshot
agent.py      the Studio agent le-chonk-qa and its tool loop: one conversation per criterion
flows.py      the Maestro flows the agent writes, kept per issue and replayed on later runs
run.py        one QA run: the release's APK, the emulator, a verdict per criterion, runs/qa/<issue>-<release>/
workflow.py   le-chonk-qa, the same run as a workflow on your Mac's deployment, le-chonk-vibe

The agent sees the issue's criteria and the running app, never the code. A verdict is pass, fail or cant_check.
A check read from the UI tree is reliable enough to block a merge; one judged from screenshots is not.
"""
