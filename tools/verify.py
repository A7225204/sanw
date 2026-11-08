"""改完跑一遍：三秒知道有没有弄坏。

**为什么写这个**：2026-11-07 那晚改 9 个文件，每改一处我都要手写探针验证，
二十行、跑完就删 —— 同一个活干了七八遍。这个脚本把那些探针固定下来。

**它抓的是"机制能不能跑通"，不是"代码有没有写错"**：

| 检查 | 判据 | 能抓住什么 |
|---|---|---|
| 语法 | 所有 .py 能 compile | 缩进/引号/半截语句 |
| 链路 | 激素→情绪→6基础→PAD→三我→表达→走神，每层有值且在范围内 | 断链、越界、KeyError |
| 消气 | 打三次 insult 再打三次 praise，**怒必须降** | 余波只涨不消（今晚那个 bug） |
| 检索 | `explain()` 有结果且 top 分 > 0.32 | 向量没建、闸门太严 |
| 自检 | `self_check.status()["ok"]` | 文件缺失、索引漂移、git 脏 |
| 前端 | room.html + 面板能开，无 console error | 白屏（今晚那个 toFixed 崩溃） |

**用法**：

    python tools/verify.py            # 全部检查
    python tools/verify.py --fast     # 跳过前端（浏览器那步最慢）
    python tools/verify.py --only 消气,检索

**退出码**：0 = 全过，1 = 有 FAIL，2 = 有 WARN 但无 FAIL。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[dict] = []


def rec(name: str, status: str, msg: str = "") -> None:
    """status: PASS / FAIL / WARN / SKIP"""
    RESULTS.append({"name": name, "status": status, "msg": msg})
    icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "SKIP": "· "}[status]
    print(f"  {icon} {name}" + (f"：{msg}" if msg else ""))


# ══════════════════════════════════════════════════════════
# ① 语法：所有 py 能 compile
# ══════════════════════════════════════════════════════════
def check_syntax() -> None:
    bad: list[str] = []
    n = 0
    for d in ("plugins", "server", "tools"):
        p = ROOT / d
        if not p.is_dir():
            continue
        for f in p.glob("*.py"):
            if f.name.startswith("bak"):        # 备份文件不查
                continue
            n += 1
            try:
                compile(f.read_text(encoding="utf-8", errors="replace"), str(f), "exec")
            except SyntaxError as e:
                bad.append(f"{f.name}:{e.lineno} {e.msg}")
            except Exception as e:  # noqa: BLE001
                bad.append(f"{f.name}: {type(e).__name__}")
    if bad:
        rec("语法", "FAIL", f"{len(bad)} 个文件有问题：" + "；".join(bad[:3]))
    else:
        rec("语法", "PASS", f"{n} 个 .py 全过")


# ══════════════════════════════════════════════════════════
# ② 链路：十四层逐层有值（含念头/心里的事/梦境/执念）
# ══════════════════════════════════════════════════════════
def check_chain() -> None:
    try:
        import nonebot
        try:
            nonebot.get_driver()
        except Exception:  # noqa: BLE001
            nonebot.init()
        from plugins import hormones as h
        from plugins import mood_compound as mc
        from plugins import pad_anchor as pa
        from plugins import attention_drift as ad
        from server import soul_aggregation as sa
    except Exception as e:  # noqa: BLE001
        rec("链路", "FAIL", f"导入失败：{type(e).__name__}: {e}")
        return

    problems: list[str] = []
    try:
        hv = h.values()
        for k in ("DA", "CORT", "SER", "OXY", "MEL"):
            if not (0 <= float(hv.get(k, -1)) <= 100):
                problems.append(f"激素 {k}={hv.get(k)} 越界")
    except Exception as e:  # noqa: BLE001
        problems.append(f"激素读不到：{type(e).__name__}")
        hv = {}

    try:
        emo = h.derive_emotions()
        if len(emo) < 5:
            problems.append(f"情绪只有 {len(emo)} 项")
        for k, v in emo.items():
            if not (0 <= float(v) <= 100):
                problems.append(f"情绪 {k}={v} 越界")
    except Exception as e:  # noqa: BLE001
        problems.append(f"情绪算不出：{type(e).__name__}")
        emo = {}

    try:
        base6 = mc._to_base(emo)
        if not base6:
            problems.append("6 基础为空")
    except Exception as e:  # noqa: BLE001
        problems.append(f"6 基础算不出：{type(e).__name__}")

    try:
        comp = mc.compound_mood(emo)
        if not comp:
            problems.append("复合情绪为空")
    except Exception as e:  # noqa: BLE001
        problems.append(f"复合情绪算不出：{type(e).__name__}")

    try:
        pad = pa.pad_from_emotions(emo)
        for ax in ("P", "A"):
            if not (-1.01 <= float(pad.get(ax, 9)) <= 1.01):
                problems.append(f"PAD.{ax}={pad.get(ax)} 越界")
    except Exception as e:  # noqa: BLE001
        problems.append(f"PAD 算不出：{type(e).__name__}")

    try:
        f = sa.forces()
        for k in ("本我", "自我", "超我"):
            v = f.get(k)
            if v is None:
                problems.append(f"三我缺 {k}")
            elif not (0 <= float(v) <= 1):
                problems.append(f"三我 {k}={v} 越界")
        # 2026-11-07 的白屏事故：forces() 里混进字符串，前端 toFixed 崩
        for k, v in f.items():
            if k in ("本我", "自我", "超我"):
                continue
            if not isinstance(v, (int, float, str)):
                problems.append(f"forces 里 {k} 类型怪：{type(v).__name__}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"三我读不到：{type(e).__name__}")

    try:
        d, msg = mc.express_depth(emo)
        if d not in (0, 1, 2, 3):
            problems.append(f"展开度 {d} 不在 0~3")
    except Exception as e:  # noqa: BLE001
        problems.append(f"展开度算不出：{type(e).__name__}")

    try:
        dr, an, re_ = ad.level()
        if dr not in ("subtle", "active", "scattered", "wild"):
            problems.append(f"走神档 {dr} 非法")
        if an not in ("strict", "balanced", "loose"):
            problems.append(f"回钩档 {an} 非法")
    except Exception as e:  # noqa: BLE001
        problems.append(f"走神算不出：{type(e).__name__}")

    try:
        pl = h.prompt_line()
        if not pl:
            problems.append("prompt_line 为空")
    except Exception as e:  # noqa: BLE001
        problems.append(f"prompt_line 挂了：{type(e).__name__}")

    # ── 2026-11-07：主人问「十层链路是不是忘了念头+执念+梦境」——
    # 确实漏了。这四样本来就进 volatile，只是没进检查。
    # 注意：空是**正常**的（不是每次都有念头/执念/梦），
    # 所以查的是「能不能取到」，不是「有没有内容」。
    # 2026-11-07 第二轮：上面那四样之外，volatile 里还有几块没进检查。
    # 完整扫了一遍 build_volatile_block + build_prompt_parts 的注入点，一共这些：
    #   状态(激素/情绪/吃饭/时间) · 对象(好感/关系) · 情绪(复合) · 当下心境(transient)
    #   今天想起(每日复习) · 此刻念头 · 心里的事 · 昨夜的梦 · 群黑话 · 别复读
    #   抖音块(罕见) · 执念 · 走神 · 阿德勒 · 需求层次 · 场景/骗局/深圳
    # 空是正常的（不是每个都有），查的是「能不能取到」。
    optional = {
        "念头": lambda: __import__("desire").thought(False),
        "心里的事": lambda: __import__("thoughts").prompt_line(),
        "梦境": lambda: __import__("plugins.dream", fromlist=["dream"]).prompt_line(),
        "执念": lambda: __import__("plugins.obsessions", fromlist=["obsessions"]).active(2),
        # ── 第二轮补的 ──
        "当下心境": lambda: __import__("plugins.ai_group", fromlist=["ai_group"]).transient_prompt(),
        "今天想起": lambda: __import__("plugins.ai_group", fromlist=["ai_group"])._daily_review_line(),
        # 2026-11-07：GROUP_MEMES 是空的（旧梗库，已废弃）—— 真正在用的是
        # group_lexicon.lexicon_brief()（那个 681 词的群黑话词典）。
        "群黑话": lambda: __import__("plugins.group_lexicon", fromlist=["group_lexicon"]).lexicon_brief("group_1109975771", 3),
        "骗局库": lambda: __import__("plugins.scam_kb", fromlist=["scam_kb"]).line("有人说要带我投资"),
        "深圳库": lambda: __import__("plugins.sz_kb", fromlist=["sz_kb"]).line("深圳租房多少钱"),
        "走神档": lambda: __import__("plugins.attention_drift", fromlist=["attention_drift"]).level(),
        "阿德勒": lambda: __import__("plugins.adler", fromlist=["adler"]).brief(),
        "需求层次": lambda: __import__("plugins.hierarchy", fromlist=["hierarchy"]).brief(),
    }
    got: list[str] = []
    for name, fn in optional.items():
        try:
            v = fn()
            got.append(f"{name}{'✓' if (v and str(v).strip()) else '·'}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"{name}取不到：{type(e).__name__}")

    if problems:
        rec("链路", "FAIL", "；".join(problems[:4]))
    else:
        rec("链路", "PASS",
            "二十层全通（激素→情绪→6基础→PAD→三我→表达→走神→" + "→".join(got) + "）")


# ══════════════════════════════════════════════════════════
# ③ 消气：被骂后必须能降（今晚那个 bug）
# ══════════════════════════════════════════════════════════
def check_deescalate() -> None:
    """打三次 insult → 怒应上升；再打三次 praise → 怒必须降下来。"""
    try:
        from plugins import hormones as h
        from plugins import emotion_residue as er
        from pathlib import Path as _P

        mem = _P(__file__).resolve().parent.parent / "memory"
        resid = mem / "emotion_residue.json"
        backup = resid.read_bytes() if resid.exists() else None
        state_f = mem / "hormones_state.json"
        st_backup = state_f.read_bytes() if state_f.exists() else None
        # 2026-11-07：adler.json 也要备份还原。
        # 「打三次 insult」会通过 emotion_residue.fire() 喂给 adler.hit()，
        # 第一次跑的时候忘了还原，结果自卑值被 verify 永久抬高到 0.83，
        # 连带「尊重」需求变成 0.67 —— 测试污染了生产数据。
        adler_f = mem / "adler.json"
        adler_backup = adler_f.read_bytes() if adler_f.exists() else None
    except Exception as e:  # noqa: BLE001
        rec("消气", "SKIP", f"导入失败：{type(e).__name__}")
        return

    try:
        # 清余波，从干净状态开始
        resid.unlink(missing_ok=True)
        for _ in range(3):
            h.apply_event("insult")
        angry_high = float(h.derive_emotions().get("怒") or 0)
        for _ in range(3):
            h.apply_event("praise")
        angry_after = float(h.derive_emotions().get("怒") or 0)

        if angry_high < 30:
            rec("消气", "WARN", f"骂了三句怒只有 {angry_high:.0f}（insult 事件可能太弱）")
        elif angry_after >= angry_high - 5:
            rec("消气", "FAIL", f"怒卡死：骂后 {angry_high:.0f} → 夸后 {angry_after:.0f}（降不下去）")
        else:
            rec("消气", "PASS", f"骂后 {angry_high:.0f} → 夸后 {angry_after:.0f}")
    except Exception as e:  # noqa: BLE001
        rec("消气", "FAIL", f"{type(e).__name__}: {e}")
    finally:
        # 还原现场：这两个文件是真实状态，不能留测试数据
        try:
            if backup is not None:
                resid.write_bytes(backup)
            elif resid.exists():
                resid.unlink()
            if st_backup is not None:
                state_f.write_bytes(st_backup)
            if adler_backup is not None:
                adler_f.write_bytes(adler_backup)
            elif adler_f.exists():
                # 原来是空的：把测试写进去的清掉，让它下次重新 seed
                adler_f.unlink()
        except Exception:  # noqa: BLE001
            pass


# ══════════════════════════════════════════════════════════
# ④ 检索：explain 有结果
# ══════════════════════════════════════════════════════════
def check_retrieval() -> None:
    try:
        from plugins import long_memory as lm
    except Exception as e:  # noqa: BLE001
        rec("检索", "FAIL", f"导入失败：{type(e).__name__}")
        return
    tests = [
        ("private_100000000", "主人钱还够吗"),
        ("private_100000000", "今天干什么了"),
    ]
    ok_n = 0
    infos: list[str] = []
    for scope, q in tests:
        try:
            rows = lm.explain(scope, q, 3)
        except Exception as e:  # noqa: BLE001
            infos.append(f"{q}: {type(e).__name__}")
            continue
        if rows and rows[0]["score"] > 0.32:
            ok_n += 1
            infos.append(f"{q}→{rows[0]['score']:.2f}")
        else:
            infos.append(f"{q}→{'空' if not rows else f'{rows[0][chr(39)+chr(39)]:.2f}'}")
    if ok_n == 0:
        rec("检索", "FAIL", "两条测试查询都没召回（向量没建？闸门太严？）")
    elif ok_n < len(tests):
        rec("检索", "WARN", "；".join(infos))
    else:
        rec("检索", "PASS", "；".join(infos))


# ══════════════════════════════════════════════════════════
# ⑤ 自检
# ══════════════════════════════════════════════════════════
def check_selfcheck() -> None:
    try:
        from plugins import self_check as sc
        t0 = time.perf_counter()
        s = sc.status()
        el = (time.perf_counter() - t0) * 1000
    except Exception as e:  # noqa: BLE001
        rec("自检", "FAIL", f"{type(e).__name__}: {e}")
        return
    if el > 2000:
        rec("自检", "WARN", f"{el:.0f} ms（超过 2 秒，说明在读全量）")
    elif not s.get("ok"):
        rec("自检", "WARN", f"{el:.0f} ms | " + "；".join(s.get("issues") or []))
    else:
        rec("自检", "PASS", f"{el:.0f} ms | 档案 {s['counts'].get('长期档案')} · 成长 {s['counts'].get('人格成长')}")


# ══════════════════════════════════════════════════════════
# ⑥ 前端：能开、无 console error
# ══════════════════════════════════════════════════════════
def check_frontend() -> None:
    shotkit = Path("C:/Users/Administrator/Doubao/chats/2026-10-20/new-chat/tools/shotkit")
    if not (shotkit / "shot.mjs").exists():
        rec("前端", "SKIP", "shotkit 不在（跳过）")
        return
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:8090/", timeout=4)
    except Exception:  # noqa: BLE001
        rec("前端", "SKIP", "面板没起（跳过）")
        return

    targets = [
        ("panel", "面板", "http://127.0.0.1:8090/"),
        ("room", "3D小家", "http://127.0.0.1:8090/memory-viz/room.html"),
        ("persona", "人格视图", "http://127.0.0.1:8090/memory-viz/"),
    ]
    msgs: list[str] = []
    fails = 0
    tmp = Path(tempfile.gettempdir())
    for slug, name, url in targets:
        try:
            # 2026-11-07：输出用 ASCII 名 + 系统临时目录绝对路径。
            # 一开始传的是 /tmp/verify_面板.png —— 中文名 + MSYS 路径，
            # 从 Python 的 subprocess 调 native node 时被转换错了，全部截图失败。
            out_png = str(tmp / f"verify_{slug}.png")
            r = subprocess.run(
                ["node", "shot.mjs", url, out_png, "--w", "1200", "--h", "800", "--wait", "8000"],
                cwd=str(shotkit), capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=90)
            line = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                msgs.append(f"{name}: 截图失败")
                fails += 1
                continue
            # 2026-11-07：favicon 的 404 是噪音（面板没配图标），但
            # consoleErrors 里那条只有 "Failed to load resource"，不带 URL ——
            # 要靠 badRequests 才看得出是 favicon。两个都要看。
            bad = [b for b in (d.get("badRequests") or []) if "favicon" not in b]
            errs = list(d.get("consoleErrors") or [])
            # 如果所有 console error 都能被 badRequests 里的 favicon 解释掉，就忽略
            if errs and not bad and all("Failed to load resource" in e for e in errs):
                errs = []
            if errs or bad:
                msgs.append(f"{name}: {(errs[0] if errs else bad[0])[:50]}")
                fails += 1
            else:
                msgs.append(f"{name} ok")
        except subprocess.TimeoutExpired:
            msgs.append(f"{name}: 超时")
            fails += 1
        except Exception as e:  # noqa: BLE001
            msgs.append(f"{name}: {type(e).__name__}")
            fails += 1
    if fails:
        rec("前端", "FAIL", "；".join(msgs))
    else:
        rec("前端", "PASS", "；".join(msgs))


# ══════════════════════════════════════════════════════════
def check_adler() -> None:
    """阿德勒的「自卑 → 补偿」能不能算出来，且在合理范围。"""
    try:
        from plugins import adler as ad
    except Exception as e:  # noqa: BLE001
        rec("阿德勒", "FAIL", f"导入失败：{type(e).__name__}")
        return
    try:
        st = ad.stats()
        bad = []
        for k in ("自卑", "补偿", "社会兴趣"):
            v = st.get(k)
            if v is None:
                bad.append(f"{k} 缺失")
            elif not (0 <= float(v) <= 1):
                bad.append(f"{k}={v} 越界")
        if st.get("风格") not in ("讨好型", "回避型", "对抗型", "平衡型"):
            bad.append(f"风格={st.get('风格')} 非法")
        if bad:
            rec("阿德勒", "FAIL", "；".join(bad))
        else:
            rec("阿德勒", "PASS", ad.brief())
    except Exception as e:  # noqa: BLE001
        rec("阿德勒", "FAIL", f"{type(e).__name__}: {e}")


def check_hierarchy() -> None:
    """马斯洛五层需求能不能算出来，权重是否按「低层压高层」走。"""
    try:
        from plugins import hierarchy as hi
        n = hi.needs()
        w = hi.weights()
        bad = []
        for k in ("生理", "安全", "归属", "尊重", "自我实现"):
            if k not in n:
                bad.append(f"缺 {k}")
            elif not (0 <= float(n[k]) <= 1):
                bad.append(f"{k}={n[k]} 越界")
            if k in w and not (0.1 <= float(w[k]) <= 1.0):
                bad.append(f"{k} 权重={w[k]} 越界")
        # 低层权重必须 >= 高层（马斯洛的单调性）
        order = [k for k, _ in hi.LEVELS]
        for i in range(len(order) - 1):
            if float(w.get(order[i]) or 0) < float(w.get(order[i + 1]) or 0) - 1e-6:
                bad.append(f"{order[i]} 权重低于 {order[i+1]}（违反单调性）")
                break
        if bad:
            rec("需求层次", "FAIL", "；".join(bad[:3]))
        else:
            rec("需求层次", "PASS", hi.brief())
    except Exception as e:  # noqa: BLE001
        rec("需求层次", "FAIL", f"{type(e).__name__}: {e}")


def check_music_ear() -> None:
    """音频结构能不能算（BPM / 段落 / 类型 / 调性 / 和弦）。

    2026-11-07：这个检查原来指向我自己新写的 `plugins/music_ear.py` ——
    写完才发现 `tools/audio_structure.py` 早就有（2026-11-04 那次会话就搬过 dsh-soundtry 的节奏+结构），
    差点维护两个干同一件事的模块。已经把手里的调性/和弦并进 audio_structure，删掉 music_ear。
    """
    from pathlib import Path as _P
    cand = [_P("B站视频/a.wav"), _P("data/audio_mix/_t_bgm.mp3")]
    f = next((x for x in cand if x.exists()), None)
    if not f:
        rec("音频结构", "SKIP", "找不到测试音频")
        return
    try:
        from tools.audio_structure import analyze
        r = analyze(str(f))
        bad = []
        bpm = float(r.get("bpm") or 0)
        if not (20 <= bpm <= 300):
            bad.append(f"BPM {bpm} 越界")
        if r.get("kind") not in ("说话为主", "唱/音乐为主", "混合"):
            bad.append(f"类型 {r.get('kind')} 非法")
        if not (r.get("sections") or []):
            bad.append("没有段落")
        if not r.get("key"):
            bad.append("没有调性")
        if not (r.get("chords") or []):
            bad.append("没有和弦")
        if bad:
            rec("音频结构", "FAIL", "；".join(bad))
        else:
            k = f"{r['bpm']:.0f}BPM {r['kind']} {r.get('key')}({r.get('key_confidence')})"
            c = "/".join(x["name"] for x in r["chords"][:3])
            rec("音频结构", "PASS", f"{k} {c} {len(r['sections'])}段")
    except Exception as e:  # noqa: BLE001
        rec("音频结构", "FAIL", f"{type(e).__name__}: {e}")


def check_model_ledger() -> None:
    """模型账本：账本里每个文件都还在、大小对得上。

    2026-11-07：那晚下 FluidR3_GM.sf2 只拿到 68 MB（头声明 148 MB），
    fluidsynth 报 Failed to load the SoundFont，查了半小时才发现是下载截断。
    这个检查就是防这个 —— 账本里记过的文件，每次都核一遍大小。
    """
    import json as _json
    from pathlib import Path as _P
    ledger = _P("data/models.json")
    if not ledger.exists():
        rec("模型账本", "SKIP", "还没有账本（tools/fetch_model.py 下完会自动建）")
        return
    try:
        d = _json.loads(ledger.read_text(encoding="utf-8"))
        if not d:
            rec("模型账本", "SKIP", "账本是空的")
            return
        bad, ok = [], 0
        for url, r in d.items():
            path = _P(r.get("path") or "")
            want = int(r.get("bytes") or 0)
            if not path.exists():
                bad.append(f"{path.name} 不见了")
                continue
            got = path.stat().st_size
            if want > 0 and got != want:
                bad.append(f"{path.name} {got}/{want}（差 {want - got}）")
            else:
                ok += 1
        if bad:
            rec("模型账本", "FAIL", "；".join(bad[:3]))
        else:
            rec("模型账本", "PASS", f"{ok} 个文件大小都对")
    except Exception as e:  # noqa: BLE001
        rec("模型账本", "FAIL", f"{type(e).__name__}: {e}")


ALL_CHECKS = {
    "语法": check_syntax,
    "模型账本": check_model_ledger,
    "音频结构": check_music_ear,
    "阿德勒": check_adler,
    "需求层次": check_hierarchy,
    "链路": check_chain,
    "消气": check_deescalate,
    "检索": check_retrieval,
    "自检": check_selfcheck,
    "前端": check_frontend,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过前端（最慢的一步）")
    ap.add_argument("--only", default="", help="逗号分隔，只跑这几项")
    args = ap.parse_args()

    want = [x.strip() for x in args.only.split(",") if x.strip()] or list(ALL_CHECKS)
    if args.fast and "前端" in want:
        want.remove("前端")

    print(f"\n验一遍（{len(want)} 项）\n" + "=" * 46)
    t0 = time.perf_counter()
    for name in want:
        fn = ALL_CHECKS.get(name)
        if not fn:
            rec(name, "SKIP", "没有这个检查")
            continue
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            rec(name, "FAIL", f"检查自己炸了：{type(e).__name__}: {e}")
    el = (time.perf_counter() - t0)

    n_fail = sum(1 for r in RESULTS if r["status"] == "FAIL")
    n_warn = sum(1 for r in RESULTS if r["status"] == "WARN")
    n_pass = sum(1 for r in RESULTS if r["status"] == "PASS")
    print("=" * 46)
    print(f"{n_pass} 过 · {n_warn} 警告 · {n_fail} 失败   用时 {el:.1f}s")

    if n_fail:
        print("\n要修的：")
        for r in RESULTS:
            if r["status"] == "FAIL":
                print(f"  · {r['name']}：{r['msg']}")
        return 1
    return 2 if n_warn else 0


if __name__ == "__main__":
    sys.exit(main())
