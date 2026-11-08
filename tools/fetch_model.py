"""下模型：下完校验字节数和 MD5，不对就续传。

**为什么写这个**：2026-11-07 那晚下 `FluidR3_GM.sf2`，
`curl -o` 拿到 **68 MB**，但 RIFF 头里声明 **148 MB** ——
文件被截断了，fluidsynth 报 `Failed to load the SoundFont`，
查了半小时才发现是下载不完整。

`curl -C -` 续传之后到 141.5 MB 才好。

**这套「声明大小 + 校验 + 续传」是 dsh-soundtry 的做法**（它 README 里写：
「权重不进仓库：仓库只记 URL、字节数和 MD5」）。我们的模型都是手动下的，一直缺这一环。

**用法**：

    # 只给 URL，第一次会下完并记下字节数和 MD5
    python tools/fetch_model.py https://example.com/model.bin

    # 给期望值，下完立刻校验
    python tools/fetch_model.py https://example.com/model.bin --bytes 148358590 --md5 abc123...

    # 只校验已有文件
    python tools/fetch_model.py --check data/soundfont/FluidR3_GM.sf2 --bytes 148358590

    # 输出到指定位置
    python tools/fetch_model.py URL -o data/soundfont/model.bin

**设计**：

    ① 先 HEAD 问 Content-Length（服务器不给就跳过大小校验）
    ② 本地已有文件 → 比大小；一样就跳过下载
    ③ 大小不对 → `curl -C -` 续传（**不是重下**，省流量）
    ④ 下完校验 MD5（给了才校验）
    ⑤ 所有结果记进 `data/models.json`（下次不用再问服务器）

**没做的事**：断点续传的进度条、多线程分片、镜像选择 —— 用不上。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "models.json"     # 下过什么、多大、MD5 多少


# ── 账本 ─────────────────────────────────────────────────
def _load() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ── 工具 ─────────────────────────────────────────────────
def _curl() -> str:
    """找一个能用的 curl（Git for Windows 自带）。"""
    for c in ("curl", shutil.which("curl") or ""):
        if c and shutil.which(c):
            return c
    raise SystemExit("找不到 curl")


def head_size(url: str, timeout: int = 20) -> int:
    """问服务器文件多大。给不出就返回 -1。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            n = r.headers.get("Content-Length")
            return int(n) if n else -1
    except Exception:  # noqa: BLE001
        # 有些服务器不支持 HEAD，退回 GET 读一行头
        try:
            req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                cr = r.headers.get("Content-Range")   # bytes 0-0/148358590
                if cr and "/" in cr:
                    return int(cr.rsplit("/", 1)[-1])
        except Exception:  # noqa: BLE001
            pass
    return -1


def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download(url: str, out: Path, expect_bytes: int = -1) -> tuple[bool, str]:
    """下（或续传）到 out。返回 (成功, 说明)。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    curl = _curl()

    have = out.stat().st_size if out.exists() else 0
    if expect_bytes > 0 and have == expect_bytes:
        return True, f"已完整（{have} 字节），跳过"
    if expect_bytes > 0 and have > expect_bytes:
        # 比期望大 = 上次下错了，从头来
        out.unlink()
        have = 0

    # 2026-11-07：**`-C -` 只在文件真的有内容时才加**。
    # 第一次写成无条件 `-C -`，实测文件不存在时会卡在「0 字节、连接被重置」——
    # curl 等着服务器给 206 Partial Content，但服务器对 0 字节的文件没有 range 可续。
    cmd = [curl, "-L", "--fail", "--retry", "3", "--retry-delay", "2"]
    if have > 0:
        # 2026-11-07 第二次踩：`cmd.insert(-2, x)` 不是「倒数第二位」的直觉 ——
        # `[a,b,c,d]` 的 insert(-2,x) 得到 `[a,b,x,c,d]`（插在 len-2 索引前），
        # 两次 insert(-2) 会把顺序颠倒成 `-o out - -C url`，
        # 于是 curl 把 `-` 当成输出文件名，内容全写进了名叫 `-` 的文件。
        # 直接拼，别玩索引。
        cmd += ["-C", "-"]
        print(f"          续传（已有 {have} 字节）")
    cmd += ["-o", str(out), url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=3600)
    except subprocess.TimeoutExpired:
        return False, "下载超时（1 小时）"

    got = out.stat().st_size if out.exists() else 0
    if r.returncode != 0 and got == 0:
        return False, f"curl 失败 rc={r.returncode}：{(r.stderr or '')[-200:]}"
    if expect_bytes > 0 and got != expect_bytes:
        return False, f"大小不对：拿到 {got}，期望 {expect_bytes}（少了 {expect_bytes - got}）"
    return True, f"下载完成 {got} 字节"


# ── 主流程 ────────────────────────────────────────────────
def fetch(url: str, out: Path | None = None,
          expect_bytes: int = -1, expect_md5: str = "") -> dict:
    led = _load()
    rec = led.get(url) or {}

    if out is None:
        name = url.rsplit("/", 1)[-1].split("?")[0] or "download.bin"
        out = ROOT / "data" / name

    # 期望值：命令行 > 账本 > HEAD
    want_bytes = expect_bytes if expect_bytes > 0 else int(rec.get("bytes") or -1)
    if want_bytes <= 0:
        want_bytes = head_size(url)
    want_md5 = (expect_md5 or str(rec.get("md5") or "")).lower()

    print(f"[下模型] {url}")
    print(f"          → {out}")
    if want_bytes > 0:
        print(f"          期望 {want_bytes} 字节（{want_bytes / 1048576:.1f} MB）")
    else:
        print("          服务器没给大小，跳过大小校验")

    ok, msg = download(url, out, want_bytes)
    print(f"          {msg}")
    if not ok:
        return {"ok": False, "why": msg, "path": str(out)}

    size = out.stat().st_size
    md5 = md5_of(out)
    rec = {"bytes": size, "md5": md5, "path": str(out)}

    if want_md5:
        if md5.lower() == want_md5:
            print(f"          MD5 ✓ {md5}")
        else:
            rec["md5_mismatch"] = want_md5
            print(f"          ❌ MD5 不对：{md5} ≠ 期望 {want_md5}")
            led[url] = rec
            _save(led)
            return {"ok": False, "why": "MD5 不匹配", "path": str(out), "md5": md5}
    else:
        print(f"          MD5 {md5}（记下来，下次就能校验）")

    led[url] = rec
    _save(led)
    return {"ok": True, "path": str(out), "bytes": size, "md5": md5}


def check(path: Path, expect_bytes: int = -1, expect_md5: str = "") -> dict:
    """只校验已有文件，不下载。"""
    if not path.exists():
        return {"ok": False, "why": "文件不存在"}
    n = path.stat().st_size
    if expect_bytes > 0 and n != expect_bytes:
        return {"ok": False, "why": f"大小不对：{n} ≠ {expect_bytes}",
                "bytes": n, "missing": expect_bytes - n}
    m = md5_of(path)
    if expect_md5 and m.lower() != expect_md5.lower():
        return {"ok": False, "why": f"MD5 不对：{m} ≠ {expect_md5}", "md5": m}
    return {"ok": True, "bytes": n, "md5": m}


def main() -> int:
    ap = argparse.ArgumentParser(description="下模型并校验（大小 + MD5 + 续传）")
    ap.add_argument("url", nargs="?", help="模型 URL")
    ap.add_argument("-o", "--out", default="", help="存到哪（默认 data/<文件名>）")
    ap.add_argument("--bytes", type=int, default=-1, help="期望字节数")
    ap.add_argument("--md5", default="", help="期望 MD5")
    ap.add_argument("--check", default="", help="只校验这个文件，不下载")
    ap.add_argument("--ledger", action="store_true", help="列出账本里记过的")
    args = ap.parse_args()

    if args.ledger:
        led = _load()
        if not led:
            print("账本是空的")
            return 0
        for u, r in led.items():
            print(f"  {r.get('bytes', 0) / 1048576:8.1f} MB  {r.get('md5', '?')[:12]}  {u}")
        return 0

    if args.check:
        r = check(Path(args.check), args.bytes, args.md5)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r.get("ok") else 1

    if not args.url:
        ap.print_help()
        return 1

    r = fetch(args.url, Path(args.out) if args.out else None, args.bytes, args.md5)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
