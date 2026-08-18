#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

GOC_BIN = Path("/tmp/goc")
GOC_SRC = Path("/tmp/goc-src")
SYSTEM_GO = "/usr/local/go/bin/go"
DEFAULT_GOPROXY = "https://proxy.golang.org,direct"

PROFILE_LINE_RE = re.compile(
    r"^(?P<path>.+?):(?P<start_line>\d+)\.(?P<start_col>\d+),"
    r"(?P<end_line>\d+)\.(?P<end_col>\d+) "
    r"(?P<statements>\d+) (?P<count>\d+)$"
)


def goc_build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GOPROXY", DEFAULT_GOPROXY)
    return env


def ensure_goc_binary() -> Path:
    if GOC_BIN.is_file() and os.access(GOC_BIN, os.X_OK):
        return GOC_BIN
    if not GOC_SRC.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/qiniu/goc.git", str(GOC_SRC)],
            check=True,
            timeout=300,
            env=goc_build_env(),
        )
    subprocess.run(
        [SYSTEM_GO, "build", "-o", str(GOC_BIN), "."],
        cwd=GOC_SRC,
        check=True,
        timeout=900,
        env=goc_build_env(),
    )
    return GOC_BIN


def center_host_port(center_url: str) -> str:
    parsed = urlparse(center_url)
    if parsed.scheme and parsed.netloc:
        return parsed.netloc
    if parsed.path:
        return parsed.path
    raise ValueError(f"unsupported goc center url: {center_url}")


def kill_stale_goc_servers(center_url: str) -> None:
    hostport = center_host_port(center_url)
    victims: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_text(errors="replace").replace("\x00", " ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if "/tmp/goc server " not in cmdline:
            continue
        if f"--port={hostport}" in cmdline:
            victims.append(int(proc.name))
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while victims and time.monotonic() < deadline:
        victims = [pid for pid in victims if Path(f"/proc/{pid}").exists()]
        if victims:
            time.sleep(0.2)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_goc_server(center_url: str, persistence: Path, log_path: Path) -> subprocess.Popen:
    goc = ensure_goc_binary()
    kill_stale_goc_servers(center_url)
    persistence.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            str(goc),
            "server",
            f"--port={center_host_port(center_url)}",
            f"--local-persistence={persistence}",
            "--ip_revise=false",
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=goc_build_env(),
    )
    proc._goc_log_fh = log_fh  # type: ignore[attr-defined]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"goc server exited early with code {proc.returncode}")
        try:
            subprocess.run(
                [str(goc), "list", f"--center={center_url}"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                env=goc_build_env(),
            )
            return proc
        except subprocess.CalledProcessError:
            time.sleep(0.3)
    raise RuntimeError(f"goc server at {center_url} did not become ready")


def stop_goc_server(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log_fh = getattr(proc, "_goc_log_fh", None)
    if log_fh:
        log_fh.close()


def run_goc(center_url: str, *args: str, timeout: int = 300,
            stdout=None, stderr=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(ensure_goc_binary()), *args, f"--center={center_url}"],
        check=True,
        timeout=timeout,
        stdout=stdout,
        stderr=stderr,
        env=goc_build_env(),
    )


def goc_init(center_url: str) -> None:
    run_goc(center_url, "init", timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def goc_clear(center_url: str) -> None:
    run_goc(center_url, "clear", timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def goc_profile(center_url: str, output: Path,
                *, services: list[str] | None = None,
                coverfiles: list[str] | None = None) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ensure_goc_binary()), "profile", f"--center={center_url}", f"--output={output}"]
    for service in services or []:
        cmd.append(f"--service={service}")
    for pattern in coverfiles or []:
        cmd.append(f"--coverfile={pattern}")
    completed = subprocess.run(
        cmd,
        timeout=300,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=goc_build_env(),
    )
    return completed.returncode == 0 and output.is_file()


def goc_merge(profiles: list[Path], output: Path) -> bool:
    valid = [Path(p) for p in profiles if Path(p).is_file() and Path(p).stat().st_size > 0]
    if not valid:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    if len(valid) == 1:
        shutil.copy2(valid[0], output)
        return True
    subprocess.run(
        [str(ensure_goc_binary()), "merge", *[str(p) for p in valid], f"--output={output}"],
        check=True,
        timeout=300,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=goc_build_env(),
    )
    return output.is_file()


def merge_into_cumulative(cumulative: Path, latest: Path) -> bool:
    if not latest.is_file() or latest.stat().st_size == 0:
        return False
    if not cumulative.exists():
        cumulative.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(latest, cumulative)
        return True
    merged = cumulative.parent / f"{cumulative.name}.tmp"
    if not goc_merge([cumulative, latest], merged):
        return False
    merged.replace(cumulative)
    return True


def compute_line_coverage(profile: Path,
                          include_patterns: list[str] | None = None) -> dict[str, float | int]:
    if not profile.is_file() or profile.stat().st_size == 0:
        return {"covered_lines": 0, "total_lines": 0, "coverage_pct": 0.0}
    total_by_file: dict[str, set[int]] = {}
    covered_by_file: dict[str, set[int]] = {}
    for raw_line in profile.read_text(errors="replace").splitlines():
        if raw_line.startswith("mode:") or not raw_line:
            continue
        match = PROFILE_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        path = match.group("path")
        if include_patterns and not any(pattern in path for pattern in include_patterns):
            continue
        start_line = int(match.group("start_line"))
        end_line = int(match.group("end_line"))
        count = int(match.group("count"))
        if end_line < start_line:
            start_line, end_line = end_line, start_line
        total_set = total_by_file.setdefault(path, set())
        covered_set = covered_by_file.setdefault(path, set())
        for line_no in range(start_line, end_line + 1):
            total_set.add(line_no)
            if count > 0:
                covered_set.add(line_no)
    total = sum(len(lines) for lines in total_by_file.values())
    covered = sum(len(covered_by_file.get(path, set())) for path in total_by_file)
    return {
        "covered_lines": covered,
        "total_lines": total,
        "coverage_pct": (covered / total * 100.0) if total else 0.0,
    }
