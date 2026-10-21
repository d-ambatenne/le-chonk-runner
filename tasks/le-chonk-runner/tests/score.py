#!/usr/bin/env python3
"""Le Chonk Runner verifier.

Scores the project in /app with the eight assertions of the kotlin-tooling-kotlin-toolchain skill's own
eval (evals.json, next to this file), so the reward is comparable with the skill's published A/B.
Harbor uploads this folder only after the agent has finished, so the agent never sees the rubric.

Writes to the output folder (/logs/verifier):
- reward.json   {"reward": share of the eight assertions passed, "<assertion>": 1 or 0, ...}
- details.json  why each assertion passed or failed, the build commands, and the environment
- build.log     the output of the verifier's own builds
- <name>.apk    with --keep-apk: the APK the verifier built, for people to install (the demo's releases)

Runs on the system Python 3 with PyYAML (python3-yaml in the image); it falls back to a tiny parser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ASSERTIONS = [
    "no_gradle_or_maven",
    "toolchain_config",
    "three_modules",
    "ios_targets",
    "android_apk",
    "shared_tests_pass",
    "shared_composable_in_both_apps",
    "dev_entry_point",
]
SKIP_DIRS = {".git", "build", ".kotlin", ".gradle", ".idea", ".amper", "DerivedData", "node_modules"}
GRADLE_OR_MAVEN = {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "pom.xml"}
IOS_TARGETS = ("iosArm64", "iosSimulatorArm64", "iosX64")
COMPOSABLE_FUN = re.compile(
    r"@Composable(?:\s*@[\w.]+(?:\([^)]*\))?)*\s+(?:(?:public|internal|private|actual|expect)\s+)*fun\s+([A-Za-z_]\w*)\s*\("
)
DEV_ENTRY_POINT = re.compile(r"@(?:org\.jetbrains\.compose\.reload\.)?DevelopmentEntryPoint\b")
OPTION_ERROR = re.compile(r"(?i)(unknown|unrecognized|no such) option|unexpected argument|got unexpected")
# Problems of the machine, not of the agent's project: reported so a run can be set aside.
ENVIRONMENT_ERRORS = {
    "android_sdk_licences": re.compile(r"(?i)licen[cs]es?\b[^\n]{0,120}\bnot (been )?accepted|sdkmanager[^\n]{0,40}--licenses"),
    "out_of_memory": re.compile(r"(?i)java\.lang\.OutOfMemoryError|Cannot allocate memory|exit code 137"),
    "network": re.compile(r"(?i)(could not resolve host|unknown ?host|connection (timed out|refused)|network is unreachable)"),
}


@dataclass
class Module:
    name: str
    path: Path
    product_type: str | None
    platforms: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def is_app(self) -> bool:
        return bool(self.product_type and self.product_type.endswith("/app"))

    @property
    def is_shared(self) -> bool:
        return bool(self.product_type) and not self.is_app and "plugin" not in (self.product_type or "")


def project_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        found.extend(Path(current) / name for name in files)
    return found


def _parse_product(text: str) -> tuple[str | None, list[str]]:
    try:
        import yaml  # python3-yaml

        data = yaml.safe_load(text) or {}
        product = data.get("product") if isinstance(data, dict) else None
        if isinstance(product, str):
            return product.strip() or None, []
        if isinstance(product, dict):
            platforms = product.get("platforms") or []
            return (str(product.get("type", "")).strip() or None), [str(p) for p in platforms] if isinstance(platforms, list) else []
        return None, []
    except ImportError:
        pass
    except Exception:
        return None, []
    # Fallback without PyYAML: `product: android/app`, or `product:` with `type:` and `platforms:` below it.
    inline = re.search(r"(?m)^product:\s*([^\s#{]+)\s*$", text)
    if inline:
        return inline.group(1), []
    block = re.search(r"(?ms)^product:\s*\n((?:[ \t]+.*\n?)+)", text)
    if not block:
        return None, []
    kind = re.search(r"(?m)^\s+type:\s*([^\s#]+)", block.group(1))
    platforms = re.search(r"(?m)^\s+platforms:\s*\[([^\]]*)\]", block.group(1))
    items = [p.strip() for p in platforms.group(1).split(",") if p.strip()] if platforms else []
    return (kind.group(1) if kind else None), items


def find_modules(root: Path, files: list[Path]) -> list[Module]:
    modules = []
    for path in files:
        if path.name != "module.yaml":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        product_type, platforms = _parse_product(text)
        modules.append(Module(name=path.parent.name, path=path.parent, product_type=product_type, platforms=platforms, text=text))
    return sorted(modules, key=lambda m: str(m.path))


def _read(paths: list[Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in paths)


def _under(path: Path, folder: Path) -> bool:
    return folder == path or folder in path.parents


def _platform_dirs(module: Module, prefix: str, *platforms: str) -> list[Path]:
    if not module.path.is_dir():
        return []
    return [d for d in module.path.iterdir() if d.is_dir() and any(d.name.startswith(f"{prefix}@{p}") for p in platforms)]


def static_checks(root: Path) -> dict[str, Any]:
    files = project_files(root)
    modules = find_modules(root, files)
    shared = [m for m in modules if m.is_shared]
    android_apps = [m for m in modules if m.product_type == "android/app"]
    ios_apps = [m for m in modules if m.product_type == "ios/app"]
    kotlin_files = [f for f in files if f.suffix == ".kt"]
    gradle_files = sorted(str(f.relative_to(root)) for f in files if f.name in GRADLE_OR_MAVEN)

    common_dirs = [m.path / "src" for m in shared if (m.path / "src").is_dir()]
    common_kt = [f for f in kotlin_files if any(_under(f, d) for d in common_dirs)]
    composables = sorted(set(COMPOSABLE_FUN.findall(_read(common_kt))))

    # An app's entry point is its own module, or the shared module's platform code (e.g. MainViewController in src@ios).
    android_dirs = [m.path for m in android_apps] + [d for m in shared for d in _platform_dirs(m, "src", "android")]
    ios_dirs = [m.path for m in ios_apps] + [d for m in shared for d in _platform_dirs(m, "src", "ios", "apple")]
    android_text = _read([f for f in files if f.suffix in {".kt", ".java"} and any(_under(f, d) for d in android_dirs)])
    ios_text = _read([f for f in files if f.suffix in {".kt", ".swift"} and any(_under(f, d) for d in ios_dirs)])
    used_in_both = [n for n in composables if re.search(rf"\b{n}\s*\(", android_text) and re.search(rf"\b{n}\s*\(", ios_text)]

    test_dirs = [m.path / "test" for m in shared if (m.path / "test").is_dir()]
    common_tests = [f for f in kotlin_files if any(_under(f, d) for d in test_dirs) and "@Test" in f.read_text(errors="replace")]
    entry_points = [str(f.relative_to(root)) for f in kotlin_files if DEV_ENTRY_POINT.search(f.read_text(errors="replace"))]

    toolchain_config = bool(modules) or (root / "project.yaml").exists()
    return {
        "assertions": {
            # "The project is built with Kotlin Toolchain, not Gradle or Maven": an empty folder doesn't pass.
            "no_gradle_or_maven": toolchain_config and not gradle_files,
            "toolchain_config": toolchain_config,
            "three_modules": bool(shared and android_apps and ios_apps),
            "ios_targets": any(t in m.text for m in modules for t in IOS_TARGETS),
            "shared_composable_in_both_apps": bool(used_in_both),
            "dev_entry_point": bool(entry_points),
        },
        "facts": {
            "modules": [
                {"name": m.name, "path": str(m.path.relative_to(root)) or ".", "product": m.product_type, "platforms": m.platforms}
                for m in modules
            ],
            "gradle_or_maven_files": gradle_files[:20],
            "wrapper": (root / "kotlin").is_file(),
            "project_yaml": (root / "project.yaml").exists(),
            "shared_composables": composables[:30],
            "composables_in_both_apps": used_in_both,
            "common_test_files": [str(f.relative_to(root)) for f in common_tests][:30],
            "dev_entry_point_files": entry_points[:10],
            "kotlin_files": len(kotlin_files),
        },
        "shared": [m.name for m in shared],
        "android": [m.name for m in android_apps],
        "ios": [m.name for m in ios_apps],
    }


def _run(command: list[str], cwd: Path, timeout: int, log: Path) -> dict[str, Any]:
    started = time.monotonic()
    env = {**os.environ, "TERM": "dumb", "NO_COLOR": "1", "KOTLIN_CLI_NO_WELCOME_BANNER": "1"}
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, env=env, start_new_session=True)
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
    text = output.decode("utf-8", errors="replace")
    seconds = round(time.monotonic() - started, 1)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(command)}\n[exit {process.returncode}, {seconds}s, timed out: {timed_out}]\n{text}\n")
    return {"command": " ".join(command), "exit_code": process.returncode, "seconds": seconds, "timed_out": timed_out, "output": text}


def _toolchain(root: Path, verb: str, module: str, timeout: int, log: Path) -> dict[str, Any]:
    wrapper = root / "kotlin"
    cli = "./kotlin" if wrapper.is_file() and os.access(wrapper, os.X_OK) else "kotlin"
    result = _run([cli, verb, "-m", module], root, timeout, log)
    if result["exit_code"] != 0 and OPTION_ERROR.search(result["output"]):
        result = _run([cli, verb], root, timeout, log)
    return result


def build_checks(root: Path, static: dict[str, Any], log: Path, keep_apk_in: Path | None = None) -> dict[str, Any]:
    assertions: dict[str, bool] = {}
    commands: dict[str, Any] = {}
    outputs: list[str] = []
    kept = None

    if static["android"]:
        started = time.time()
        result = _toolchain(root, "build", static["android"][0], 1500, log)
        apks = [p for p in root.rglob("*.apk") if p.stat().st_mtime >= started - 2]
        assertions["android_apk"] = result["exit_code"] == 0 and bool(apks)
        commands["android"] = {k: result[k] for k in ("command", "exit_code", "seconds", "timed_out")}
        commands["android"]["apks"] = [str(p.relative_to(root)) for p in apks[:5]]
        outputs.append(result["output"])
        if keep_apk_in is not None and assertions["android_apk"]:
            newest = max(apks, key=lambda p: p.stat().st_mtime)
            shutil.copy2(newest, keep_apk_in / newest.name)
            kept = newest.name
    else:
        assertions["android_apk"] = False

    if static["shared"] and static["facts"]["common_test_files"]:
        result = _toolchain(root, "test", static["shared"][0], 900, log)
        assertions["shared_tests_pass"] = result["exit_code"] == 0
        commands["tests"] = {k: result[k] for k in ("command", "exit_code", "seconds", "timed_out")}
        outputs.append(result["output"])
    else:
        assertions["shared_tests_pass"] = False

    joined = "\n".join(outputs)
    errors = [name for name, pattern in ENVIRONMENT_ERRORS.items() if pattern.search(joined)]
    return {"assertions": assertions, "commands": commands, "environment_errors": errors, "apk": kept}


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _wrapper_version(root: Path) -> str | None:
    wrapper = root / "kotlin"
    if not wrapper.is_file():
        return None
    match = re.search(r"(?m)^\s*(?:set\s+)?\w*version\w*=[\"']?([0-9][\w.\-+]*)", wrapper.read_text(errors="replace"), re.I)
    return match.group(1) if match else None


def score(root: Path, out: Path, run_builds: bool = True, evals_path: Path | None = None, keep_apk: bool = False) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    log = out / "build.log"
    static = static_checks(root)
    assertions: dict[str, bool | None] = dict(static["assertions"])
    builds: dict[str, Any] = {"commands": {}, "environment_errors": [], "apk": None}
    if run_builds and root.is_dir():
        builds = build_checks(root, static, log, keep_apk_in=out if keep_apk else None)
        assertions.update(builds["assertions"])
    else:
        assertions.update({"android_apk": None, "shared_tests_pass": None})

    passed = sum(1 for key in ASSERTIONS if assertions.get(key))
    reward = round(passed / len(ASSERTIONS), 4)
    labels = {}
    if evals_path and evals_path.is_file():
        texts = json.loads(evals_path.read_text())["evals"][0]["assertions"]
        labels = dict(zip(ASSERTIONS, texts))

    rewards = {"reward": reward, **{key: int(bool(assertions.get(key))) for key in ASSERTIONS}}
    details = {
        "reward": reward,
        "assertions": assertions,
        "complete": all(assertions.get(key) is not None for key in ASSERTIONS),
        "facts": static["facts"],
        "commands": builds["commands"],
        "environment_errors": builds["environment_errors"],
        "apk": builds["apk"],
        "environment": {
            "kotlin_toolchain": _read_text("/etc/kotlin-toolchain-version"),
            "project_wrapper_version": _wrapper_version(root) if root.is_dir() else None,
            "warmup": _read_text("/etc/le-chonk-warmup"),
        },
        "labels": labels,
    }
    (out / "reward.json").write_text(json.dumps(rewards), encoding="utf-8")
    (out / "details.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
    return details


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default="/app")
    parser.add_argument("--out", default="/logs/verifier")
    parser.add_argument("--no-build", action="store_true", help="static checks only")
    parser.add_argument("--evals", default=str(Path(__file__).with_name("evals.json")))
    parser.add_argument("--keep-apk", action="store_true", help="copy the APK the build made to the output folder")
    args = parser.parse_args()
    details = score(Path(args.project), Path(args.out), run_builds=not args.no_build, evals_path=Path(args.evals), keep_apk=args.keep_apk)
    print(f"reward {details['reward']:.2f}")
    for key in ASSERTIONS:
        value = details["assertions"].get(key)
        mark = "n/a " if value is None else ("pass" if value else "FAIL")
        print(f"  {mark}  {key}")
    if details["environment_errors"]:
        print("environment errors: " + ", ".join(details["environment_errors"]))
    print("environment: " + json.dumps(details["environment"]))


if __name__ == "__main__":
    main()
