#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_apk_all.py - 并行执行 npm 与 Git 两条发布通道。

两个子脚本互不依赖（npm 写 registry，git 写 QGB/suishou），可以同时跑：
- 两边共用一个 APK，都自带“内容未变化则跳过”的保护；
- 输出按 [npm] / [git] 前缀实时拼接；
- 任一失败则本脚本返回非 0（不会冒充成功）。

用法:

    python3 publish_apk_all.py                 # 并行发布（APK 未变则两边自动跳过）
    python3 publish_apk_all.py --dry-run       # 两边都演练，不上传
    python3 publish_apk_all.py --force         # APK 没变也强制重发
    python3 publish_apk_all.py --apk out/xxx.apk --branch main
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def pump(proc: subprocess.Popen, prefix: str) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        print("[%s] %s" % (prefix, line.rstrip("\n")), flush=True)
    try:
        proc.stdout.close()
    except Exception:  # noqa: BLE001
        pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="并行发布 APK 到 npm 与 GitHub（固定名 ime.apk）。")
    ap.add_argument("--apk", default=None, help="要发布的 APK（默认 out/ 下最新）")
    ap.add_argument("--force", action="store_true", help="APK 未变化也强制重发")
    ap.add_argument("--dry-run", action="store_true", help="两边都只演练，不上传")
    ap.add_argument("--verify-timeout", type=float, default=420.0,
                    help="npm 发布后回查 registry 的最长等待秒数（默认 420）")
    ap.add_argument("--repo", default=None, help="Git 仓库（默认 git@ssh.github.com:QGB/suishou）")
    ap.add_argument("--branch", default=None, help="Git 分支（默认 main）")
    ap.add_argument("--dir", default=None, help="Git 本地工作副本")
    ap.add_argument("--key", default=None, help="SSH 私钥（默认 ~/.ssh/NIST256p.pem）")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    npm_cmd = [sys.executable, str(SCRIPTS / "publish_apk_npm.py"),
               "--verify-timeout", str(args.verify_timeout)]
    git_cmd = [sys.executable, str(SCRIPTS / "publish_apk_git.py")]
    for cmd in (npm_cmd, git_cmd):
        if args.apk:
            cmd += ["--apk", args.apk]
        if args.force:
            cmd.append("--force")
        if args.dry_run:
            cmd.append("--dry-run")
    for flag, value in (("--repo", args.repo), ("--branch", args.branch),
                        ("--dir", args.dir), ("--key", args.key)):
        if value:
            git_cmd += [flag, value]

    print("[all] 并行启动两条通道", flush=True)
    print("[all] npm: " + " ".join(npm_cmd[1:]), flush=True)
    print("[all] git: " + " ".join(git_cmd[1:]), flush=True)

    started = time.monotonic()
    procs = {
        "npm": subprocess.Popen(npm_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1),
        "git": subprocess.Popen(git_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1),
    }
    threads = [threading.Thread(target=pump, args=(proc, name), daemon=True)
               for name, proc in procs.items()]
    for t in threads:
        t.start()
    try:
        for name, proc in procs.items():
            proc.wait()
    except KeyboardInterrupt:
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            proc.wait()
        for t in threads:
            t.join(timeout=5)
        print("[all] 已中断", flush=True)
        return 130
    for t in threads:
        t.join(timeout=5)

    elapsed = time.monotonic() - started
    rcs = {name: proc.returncode for name, proc in procs.items()}
    print("\n[all] 完成，用时 %.1fs，npm rc=%s，git rc=%s"
          % (elapsed, rcs["npm"], rcs["git"]), flush=True)
    return 0 if all(rc == 0 for rc in rcs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
