#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_apk_npm.py - 把 xime 的 APK 作为「新版本」追加到 npm 包 qgb。

远端只需 Python 3 标准库 + 已安装的 node/npm（本机实测 node v20.20.2 / npm 10.8.2）。

典型用法（推荐在 /root/build_xime_home/xime/scripts 下执行）:

    # 0) 看 npm 上已有版本（只读）
    python3 publish_apk_npm.py --list-versions

    # 1) 演练：打包 + 校验体积/文件清单/版本号，不上传（无需 token）
    python3 publish_apk_npm.py --dry-run

    # 2) 正式发布（token 来自 NPM_TOKEN 或 ~/.npmrc）
    python3 publish_apk_npm.py

    # 3) 指定 APK / 包内文件名 / 版本 / dist-tag
    python3 publish_apk_npm.py --apk out/com.qgb.xime-20260920-arm64-v8a.apk \
        --name ime.apk --version 0.0.7 --tag latest

    # 4) 把 token 写进 ~/.npmrc（chmod 600），之后不带 token 也能发布
    python3 publish_apk_npm.py --save-token npm_xxxxxxxx

    # 5) token 未勾选 Bypass 2FA 时，临时附一次性验证码
    python3 publish_apk_npm.py --otp 123456

行为约定:
- 默认 APK = out/ 下最新的 com.qgb.xime-*-arm64-v8a.apk。
- 包内文件名固定为 ime.apk（不带版本号，便于 CDN 用 @latest 直链）。
- 默认版本 = registry 上该包已有的最高版本 patch + 1。
- 发布前先在 APK 同文件系统的临时目录里 npm pack 成 tgz，核对体积后再
  `npm publish <tgz>`；发布后回查 registry 的 dist.shasum 做一致性校验。
- npm 对大包会异步处理（PUT 返回 202），所以回查按总超时轮询。
- token 不写进本脚本；只接受 --token / NPM_TOKEN / NODE_AUTH_TOKEN / ~/.npmrc。
- 临时目录（含 stage 和 tgz）在结束时删除，--keep-stage 可保留排查。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PACKAGE = "qgb"
DEFAULT_REGISTRY = "https://registry.npmjs.org"
DEFAULT_TAG = "latest"
# npm 包内的 APK 文件名固定为 ime.apk（不带版本号，便于 CDN 用 @latest 直链）
DEFAULT_PUBLISH_NAME = "ime.apk"
DEFAULT_OUT_DIR = Path("/root/build_xime_home/xime/out")
APK_PATTERN = "com.qgb.xime-*-arm64-v8a.apk"
# registry 对单个 tgz 的常见上限是 100MiB；超过先拦一下，确认后可用 --allow-large 放行
MAX_TARBALL_MIB = 100.0


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 2) -> "None":
    print("ERROR: " + msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%dB" % n if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fGiB" % n


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sha1_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- token / npmrc

def npmrc_path() -> Path:
    env = os.environ.get("NPM_CONFIG_USERCONFIG") or ""
    return Path(env).expanduser() if env.strip() else Path.home() / ".npmrc"


def read_npmrc_token() -> "str | None":
    p = npmrc_path()
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "_authToken" not in line:
            continue
        m = re.match(r".*_authToken\s*=\s*(\S+)", line)
        if m:
            return m.group(1).strip()
    return None


def resolve_token(explicit: "str | None") -> "tuple[str | None, str]":
    if explicit and explicit.strip():
        return explicit.strip(), "--token"
    for key in ("NPM_TOKEN", "NODE_AUTH_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip(), key
    tok = read_npmrc_token()
    if tok:
        return tok, str(npmrc_path())
    return None, ""


def save_token(token: str, registry: str) -> Path:
    token = token.strip()
    if not token:
        die("--save-token 的 token 为空")
    host = urllib.parse.urlparse(registry).netloc or "registry.npmjs.org"
    p = npmrc_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines, replaced = [], False
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if re.match(r"\s*//%s/:_authToken\s*=" % re.escape(host), line):
                lines.append("//%s/:_authToken=%s" % (host, token))
                replaced = True
            elif line.strip():
                lines.append(line)
    if not replaced:
        lines.append("//%s/:_authToken=%s" % (host, token))
    if not any(l.startswith("registry=") for l in lines):
        lines.append("registry=%s/" % registry.rstrip("/"))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def write_temp_npmrc(token: str, registry: str, workdir: Path) -> Path:
    host = urllib.parse.urlparse(registry).netloc or "registry.npmjs.org"
    p = workdir / ".npmrc"
    p.write_text(
        "registry=%s/\n//%s/:_authToken=%s\n" % (registry.rstrip("/"), host, token),
        encoding="utf-8",
    )
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


# ---------------------------------------------------------------- registry query

def packument_url(registry: str, pkg: str) -> str:
    return registry.rstrip("/") + "/" + urllib.parse.quote(pkg, safe="@")


def fetch_json(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "xime-publish-apk/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_packument(registry: str, pkg: str, timeout: float = 30.0) -> dict:
    try:
        return fetch_json(packument_url(registry, pkg), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    except urllib.error.URLError as exc:
        die("无法访问 registry %s：%s（检查网络/代理）" % (registry, exc))


_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def semver_tuple(ver: str) -> "tuple[int, int, int] | None":
    m = _RELEASE_RE.match(ver.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def choose_next_version(existing: "list[str]") -> str:
    releases = [v for v in existing if semver_tuple(v)]
    if not releases:
        return "1.0.0"
    major, minor, patch = max((semver_tuple(v) for v in releases))
    while True:
        patch += 1
        cand = "%d.%d.%d" % (major, minor, patch)
        if cand not in existing:
            return cand


# ---------------------------------------------------------------- apk / staging

def find_latest_apk(out_dir: Path) -> "Path | None":
    if not out_dir.is_dir():
        return None
    cands = [p for p in out_dir.glob(APK_PATTERN) if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: (p.stat().st_mtime, p.name))


def build_stage(apk: Path, pkg: str, version: str, tag: str,
                pub_name: str = DEFAULT_PUBLISH_NAME) -> "tuple[Path, str]":
    """在临时目录组装 npm 包；包内文件名固定为 pub_name（默认 ime.apk）。"""
    # 放在 APK 同目录（同一文件系统）-> 硬链接，零拷贝；不支持时退回复制
    stage = Path(tempfile.mkdtemp(prefix=".npm-publish-", dir=str(apk.parent)))
    target = stage / pub_name
    try:
        os.link(apk, target)
    except OSError:
        shutil.copy2(apk, target)

    digest = sha256_file(apk)
    size = apk.stat().st_size
    (stage / "SHA256SUMS.txt").write_text("%s  %s\n" % (digest, pub_name), encoding="utf-8")

    readme = """# {pkg} - xime APK 分发

本版本只分发 Android 安装包，不含 JavaScript 入口（历史版本里的 `qgb.js` 未保留）。

| 项 | 值 |
| --- | --- |
| 版本 | `{version}` |
| dist-tag | `{tag}` |
| 文件 | `{name}`（{size}） |
| 原始构建产物 | `{src}` |
| SHA256 | `{sha}` |
| 生成时间 | {when} |

## 获取

```bash
npm pack {pkg}@{version}     # 得到 tgz，解包后即为 {name}
```

或直接下载（文件名不带版本号，配 `@latest` 始终是同一个链接）：

```
https://unpkg.com/{pkg}@latest/{name}
https://cdn.jsdelivr.net/npm/{pkg}@latest/{name}   # 注意 jsDelivr npm 单文件上限 50MB
{reg_tarball}
```

解包后先用 `SHA256SUMS.txt` 校验再安装：

```bash
sha256sum -c SHA256SUMS.txt
adb install -r {name}
```
""".format(pkg=pkg, version=version, tag=tag, name=pub_name, src=apk.name,
           size=human_size(size), sha=digest,
           when=time.strftime("%Y-%m-%d %H:%M:%S"),
           reg_tarball="https://registry.npmjs.org/%s/-/%s-%s.tgz" % (pkg, pkg, version))
    (stage / "README.md").write_text(readme, encoding="utf-8")

    manifest = {
        "name": pkg,
        "version": version,
        "description": "qgb xime APK distribution (arm64-v8a, file %s)" % pub_name,
        "keywords": ["qgb", "xime", "apk", "android", "distribution"],
        "files": [pub_name, "SHA256SUMS.txt", "README.md"],
    }
    if pkg.startswith("@"):
        manifest["publishConfig"] = {"access": "public"}
    (stage / "package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stage, digest


# ---------------------------------------------------------------- npm glue

def npm_exe() -> str:
    exe = shutil.which("npm")
    if not exe:
        die("找不到 npm，请先安装 Node.js（本机应有 node/npm）")
    return exe


def run_npm(args: "list[str]", cwd: Path, timeout: float,
            extra_env: "dict[str, str] | None" = None) -> subprocess.CompletedProcess:
    cmd = [npm_exe()] + list(args)
    env = os.environ.copy()
    env.setdefault("npm_config_fund", "false")
    env.setdefault("npm_config_audit", "false")
    if extra_env:
        env.update(extra_env)
    log("$ npm " + " ".join(args))
    try:
        return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        die("npm 执行超时（%.0fs）：%s" % (timeout, " ".join(args)), code=1)


def npm_pack(stage: Path, timeout: float = 900.0) -> dict:
    proc = run_npm(["pack", "--json", "--ignore-scripts", "--pack-destination", str(stage)],
                   cwd=stage, timeout=timeout)
    out = proc.stdout or ""
    if proc.returncode != 0:
        die("npm pack 失败（rc=%s）\nstdout:\n%s\nstderr:\n%s"
            % (proc.returncode, out.strip(), (proc.stderr or "").strip()), code=1)
    start, end = out.find("["), out.rfind("]")
    if start < 0 or end < start:
        die("npm pack 输出不是 JSON：%s" % out.strip(), code=1)
    try:
        data = json.loads(out[start:end + 1])
    except json.JSONDecodeError as exc:
        die("解析 npm pack JSON 失败：%s\n%s" % (exc, out.strip()), code=1)
    if not data or not isinstance(data, list):
        die("npm pack 未产出 tgz：%s" % out.strip(), code=1)
    return data[0]


def npm_publish(tgz: Path, tag: str, registry: str, npmrc: Path,
                timeout: float, otp: "str | None" = None) -> subprocess.CompletedProcess:
    args = ["publish", str(tgz), "--tag", tag,
            "--registry", registry.rstrip("/"), "--ignore-scripts",
            "--userconfig", str(npmrc)]
    if otp:
        args += ["--otp", otp]
    return run_npm(args, cwd=tgz.parent, timeout=timeout,
                   extra_env={"npm_config_userconfig": str(npmrc)})


def verify_published(pkg: str, version: str, shasum: str, registry: str,
                     timeout: float = 300.0, interval: float = 5.0) -> str:
    """回查 registry 上的版本元数据。返回 ok / mismatch / pending。

    npm 对大包会异步处理（PUT 返回 202，并提示“需要几分钟才能生效”），
    所以这里按总超时轮询，而不是固定重试几次。
    """
    url = packument_url(registry, pkg) + "/" + urllib.parse.quote(version, safe="")
    deadline = time.monotonic() + max(0.0, timeout)
    delay = 2.0
    attempt = 0
    while True:
        attempt += 1
        try:
            doc = fetch_json(url)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                log("  回查 HTTP %s，%.0fs 后重试…" % (exc.code, delay))
        except Exception as exc:  # noqa: BLE001 - 网络抖动统一重试
            log("  回查异常（%s），%.0fs 后重试…" % (exc, delay))
        else:
            dist = doc.get("dist") or {}
            got = dist.get("shasum")
            if got == shasum:
                log("  校验通过：dist.shasum=%s" % got)
                log("  tarball: %s" % dist.get("tarball"))
                return "ok"
            log("  shasum 不一致：registry=%s 本地=%s" % (got, shasum))
            return "mismatch"

        if time.monotonic() + delay >= deadline:
            log("  已等待 %.0fs，版本仍未生效" % timeout)
            return "pending"
        log("  [%d] 版本尚未生效，%.0fs 后重试…" % (attempt, delay))
        time.sleep(delay)
        delay = min(delay * 1.6, 20.0)


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="把 xime 的 APK 作为新版本追加到 npm 包（默认 qgb）。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apk", "--file", dest="apk", default=None,
                    help="要发布的文件（默认 out/ 下最新的 %s）" % APK_PATTERN)
    ap.add_argument("--name", default=DEFAULT_PUBLISH_NAME,
                    help="npm 包内的文件名（默认 %s，不带版本号）" % DEFAULT_PUBLISH_NAME)
    ap.add_argument("--package", default=DEFAULT_PACKAGE, help="npm 包名（默认 %s）" % DEFAULT_PACKAGE)
    ap.add_argument("--registry", default=DEFAULT_REGISTRY, help="registry（默认 %s）" % DEFAULT_REGISTRY)
    ap.add_argument("--version", default=None,
                    help="要发布的版本；默认 = registry 已有最高版本 patch + 1")
    ap.add_argument("--tag", default=DEFAULT_TAG, help="dist-tag（默认 %s）" % DEFAULT_TAG)
    ap.add_argument("--token", default=None,
                    help="npm token；也可用环境变量 NPM_TOKEN / NODE_AUTH_TOKEN 或 ~/.npmrc")
    ap.add_argument("--otp", default=None,
                    help="2FA 一次性验证码；仅当 token 未开启 Bypass 2FA 时需要（npm publish --otp）")
    ap.add_argument("--save-token", default=None, metavar="TOKEN",
                    help="把 token 写入 ~/.npmrc（chmod 600）后退出")
    ap.add_argument("--dry-run", action="store_true", help="只打包校验，不上传")
    ap.add_argument("--list-versions", action="store_true", help="列出 registry 上已有版本后退出")
    ap.add_argument("--keep-stage", action="store_true", help="保留临时打包目录")
    ap.add_argument("--allow-large", action="store_true",
                    help="允许 tgz 超过 %.0fMiB 仍继续发布" % MAX_TARBALL_MIB)
    ap.add_argument("--timeout", type=float, default=1800.0, help="npm pack/publish 超时秒数")
    ap.add_argument("--verify-timeout", type=float, default=300.0,
                    help="发布后回查 registry 的最长等待秒数（大包异步处理可能要几分钟）")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    if args.save_token:
        p = save_token(args.save_token, args.registry)
        log("已写入 %s（chmod 600）" % p)
        return 0

    # 1) 定位文件
    if args.apk:
        apk = Path(args.apk).expanduser()
    else:
        apk = find_latest_apk(DEFAULT_OUT_DIR)
        if apk is None:
            die("在 %s 下找不到 %s，请用 --apk 指定" % (DEFAULT_OUT_DIR, APK_PATTERN))
    if not apk.is_file():
        die("文件不存在：%s" % apk)
    if apk.stat().st_size == 0:
        die("文件为空：%s" % apk)

    log("APK      : %s (%s)" % (apk, human_size(apk.stat().st_size)))

    # 2) 查已有版本
    pkgdoc = fetch_packument(args.registry, args.package)
    existing = sorted((pkgdoc.get("versions") or {}).keys(),
                      key=lambda v: (semver_tuple(v) or (0, 0, 0), v))
    log("包       : %s @ %s（已有 %d 个版本：%s）"
        % (args.package, args.registry, len(existing),
           ", ".join(existing) if existing else "无"))
    if args.list_versions:
        for v in existing:
            log("  - %s" % v)
        return 0

    # 3) 版本
    if args.version:
        version = args.version.strip()
        if not semver_tuple(version):
            die("--version 必须是 x.y.z 形式：%s" % version)
        if version in existing:
            die("%s@%s 已发布，不能再覆盖；换版本或让它自动 +1" % (args.package, version))
    else:
        version = choose_next_version(existing)
    log("新版本   : %s@%s (tag=%s, 包内文件=%s)" % (args.package, version, args.tag, args.name))

    # 4) 打包
    stage, digest = build_stage(apk, args.package, version, args.tag, args.name)
    try:
        meta = npm_pack(stage, timeout=args.timeout)
        tgz_path = stage / meta.get("filename", "%s-%s.tgz" % (args.package, version))
        if not tgz_path.is_file():
            die("npm pack 未生成 tgz：%s" % tgz_path, code=1)
        tgz_size = tgz_path.stat().st_size
        shasum = meta.get("shasum") or sha1_file(tgz_path)
        integrity = meta.get("integrity") or ""
        log("tgz      : %s (%s)" % (tgz_path.name, human_size(tgz_size)))
        log("shasum   : %s" % shasum)
        if integrity:
            log("integrity: %s" % integrity)
        log("APK SHA256: %s" % digest)
        log("包内文件 : %s" % ", ".join(sorted(
            [f["path"] for f in meta["files"]] if meta.get("files") else [args.name])))

        if tgz_size > MAX_TARBALL_MIB * 1024 * 1024 and not args.allow_large:
            die("tgz 有 %s，超过 %.0fMiB 的常规上限；确认要传就加 --allow-large"
                "(registry 仍可能回 413)" % (human_size(tgz_size), MAX_TARBALL_MIB))

        if args.dry_run:
            log("\n[dry-run] 不执行 npm publish。确认无误后去掉 --dry-run 再跑一次。")
            return 0

        # 5) token
        token, source = resolve_token(args.token)
        if not token:
            die("缺少 npm token：用 --token、环境变量 NPM_TOKEN，"
                "或先 `--save-token <token>` 写进 ~/.npmrc")
        log("token 来源: %s" % source)
        npmrc = write_temp_npmrc(token, args.registry, stage)

        # 6) 发布
        proc = npm_publish(tgz_path, args.tag, args.registry, npmrc,
                           timeout=args.timeout, otp=args.otp)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            out = (proc.stdout or "").strip()
            hint = ""
            low = err.lower()
            if "two-factor" in low or "bypass 2fa" in low:
                hint = ("\n提示：npm 账号开了 2FA，而当前 token 没有勾选 “Bypass 2FA”。\n"
                        "      A. 到 https://www.npmjs.com/settings/<账号>/tokens 重新生成\n"
                        "         granular token（Permissions: Read and write，"
                        "勾上 Bypass 2FA）；\n"
                        "      B. 或本机现输一次性验证码：publish_apk_npm.py --otp 123456")
            elif "e403" in low or "403" in err:
                hint = ("\n提示：403 多为 token 无该包写权限/版本已存在。"
                        "检查 npm owner ls %s 与 token 权限。" % args.package)
            die("npm publish 失败（rc=%s）%s\nstdout:\n%s\nstderr:\n%s"
                % (proc.returncode, hint, out, err), code=1)
        if (proc.stdout or "").strip():
            log((proc.stdout or "").strip())

        # 7) 回查校验（大包会被 registry 异步处理：PUT 返回 202）
        log("\n回查 registry 校验（最多等 %.0fs）…" % args.verify_timeout)
        status = verify_published(args.package, version, shasum, args.registry,
                                  timeout=args.verify_timeout)
        if status == "ok":
            log("\n发布成功：%s@%s" % (args.package, version))
        elif status == "mismatch":
            log("\n[WARN] 已发布，但 tarball shasum 与 registry 记录不一致，请人工核查")
        else:
            log("\n[WARN] 已提交成功（npm 返回 202：包在异步处理），registry 尚未生效；"
                "过几分钟复查：")
        log("  npm view %s@%s dist.tarball" % (args.package, version))
        log("  安装包下载: %s/%s/-/%s-%s.tgz"
            % (args.registry.rstrip("/"), args.package, args.package, version))
        return 0 if status in ("ok", "pending") else 3
    finally:
        if args.keep_stage:
            log("保留临时目录：%s" % stage)
        else:
            shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
