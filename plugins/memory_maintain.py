"""长期记忆维护批处理（借鉴 SeekMemory 的 DreamCycle 维护段）。

做三件事，都带预算、可试运行：
    1. 合并近似重复（复用 long_memory._dedupe_scope，阈值 0.9）
    2. 遗忘：生效权重低于 MEM_DECAY_FLOOR 且没 pin 的条目 → 挪进 memory/forgotten.json（不直接删）
    3. 记账：每次跑完写 memory/maint_ledger.json（日期/次数/合并/遗忘/是否 apply）

cli:
    python tools/memory_maintain.py            # 试运行，只报数
    python tools/memory_maintain.py --apply    # 真跑
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FORGOTTEN = ROOT / "memory" / "forgotten.json"
LEDGER = ROOT / "memory" / "maint_ledger.json"

MAX_SCOPES = 40          # 单次最多扫多少个 scope
MAX_FORGET = 60          # 单次最多遗忘多少条（预算）
DEDUPE_TH = 0.90


def _load_forgotten() -> list:
    try:
        return json.loads(FORGOTTEN.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []


def _ledger_add(row: dict) -> None:
    try:
        rows = json.loads(LEDGER.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        rows = []
    rows.append(row)
    LEDGER.write_text(json.dumps(rows[-200:], ensure_ascii=False, indent=1), encoding="utf-8")


_RE_SCENE = re.compile(r"今天|昨天|刚才|这周|上周|下午|晚上|早上|那次|当时|那天|看到|去了|吃了|喝了|发生|开会|加班")
_RE_IMPR = re.compile(r"觉得|感觉|印象|认为|好像|似乎|评价|看起来|挺|有点|讨厌|害怕|烦|不爽|喜欢|不喜欢")
_RE_PERSONA = re.compile(r"总是|从来|习惯|性格|原则|坚持|说话|口癖|信念|价值观")
# 敏感度：2 = 私密（18+/隐私），1 = 私域（关系、情绪、矛盾），0 = 公开
_RE_SENS2 = re.compile(r"18\+|R18|r18|色情|裸|性爱|做爱|胸|臀|情趣|床戏|自慰|勃起|内裤|内衣|密码|身份证|银行卡|住址|手机号|工资")
_RE_SENS1 = re.compile(r"讨厌|心情|委屈|生气|吵架|挂人|黑料|八卦|秘密|偷偷|害怕|哭|难受|思念")


def classify(apply: bool = True) -> dict:
    """给记忆打类型（fact/scene/impression/persona）与敏感度（0/1/2）。

    已是 cls+sens 齐全的跳过；缺哪项补哪项，所以可以反复跑。
    """
    from plugins import long_memory as lm
    counts = {"fact": 0, "scene": 0, "impression": 0, "persona": 0,
              "sens0": 0, "sens1": 0, "sens2": 0, "skipped": 0}
    self_names = ("本鱼", "大肥鱼", "鲸鱼娘", "她")
    for sc, items in list(lm._archive.items()):
        for it in items or []:
            has_cls = bool(it.get("cls"))
            has_sens = "sens" in it
            if has_cls and has_sens:
                counts["skipped"] += 1
                continue
            subj = str(it.get("subject") or "")
            text = subj + str(it.get("fact") or "")
            if not has_cls:
                if subj in self_names and _RE_PERSONA.search(text):
                    cls = "persona"
                elif _RE_SCENE.search(text):
                    cls = "scene"
                elif _RE_IMPR.search(text):
                    cls = "impression"
                else:
                    cls = "fact"
                counts[cls] += 1
                if apply:
                    it["cls"] = cls
            if not has_sens:
                sens = 2 if _RE_SENS2.search(text) else (1 if _RE_SENS1.search(text) else 0)
                counts["sens%d" % sens] += 1
                if apply:
                    it["sens"] = sens
    if apply:
        try:
            lm._save()
        except Exception:  # noqa: BLE001
            pass
    return counts


def classify_growth(apply: bool = True) -> int:
    """把人格成长条目也纳入分类口径：成长条目本身就是 persona 级。"""
    try:
        import json as _json
        p = ROOT / "memory" / "persona_growth.json"
        d = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        n = 0
        for it in d.get("items") or []:
            if it.get("cls") == "persona":
                continue
            if apply:
                it["cls"] = "persona"
            n += 1
        if apply and n:
            p.write_text(_json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        return n
    except Exception:  # noqa: BLE001
        return 0


def recalibrate(apply: bool = True) -> dict:
    """清掉旧 sens 后按当前规则重算（规则改窄了以后用这个刷一遍）。"""
    from plugins import long_memory as lm
    for _sc, items in list(lm._archive.items()):
        for it in items or []:
            it.pop("sens", None)
    before = None
    out = classify(apply=apply)
    return {"classes": out, "before": before}


def run(apply: bool = False) -> dict:
    """跑一轮维护；apply=False 只统计。返回统计结果。"""
    from plugins import long_memory as lm

    t0 = time.time()
    cls_counts = classify(apply=apply)
    _g = classify_growth(apply=apply)
    if _g:
        cls_counts["growth_persona"] = _g
    active = [(sc, items) for sc, items in lm._archive.items() if items]
    active.sort(key=lambda x: -len(x[1]))
    active = active[:MAX_SCOPES]

    merged_total = 0
    forgotten: list[dict] = []
    for sc, items in active:
        if apply:
            try:
                merged_total += int(lm._dedupe_scope(sc, DEDUPE_TH) or 0)
            except Exception:  # noqa: BLE001
                pass
        else:
            # 试运行：只数有多少对会被合并
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    try:
                        if lm._similarity(a.get("fact") or "", b.get("fact") or "") >= DEDUPE_TH:
                            merged_total += 1
                    except Exception:  # noqa: BLE001
                        continue
        now = lm._now_ts()
        day = (now // 86400) * 86400
        weak = []
        for it in items:
            if it.get("pin"):
                continue
            try:
                if lm._item_rank(it, day) < lm.MEM_DECAY_FLOOR:
                    weak.append(it)
            except Exception:  # noqa: BLE001
                continue
        if apply and weak:
            weak = weak[:MAX_FORGET]
            drop_ids = {it.get("id") for it in weak}
            lm._archive[sc] = [it for it in items if it.get("id") not in drop_ids]
            forgotten += [dict(it, _scope=sc) for it in weak]
        else:
            forgotten += weak

    if apply:
        try:
            lm._save()
        except Exception:  # noqa: BLE001
            pass
        if forgotten:
            old = _load_forgotten()
            old += forgotten[-MAX_FORGET:]
            FORGOTTEN.write_text(json.dumps(old[-2000:], ensure_ascii=False, indent=1),
                                 encoding="utf-8")

    row = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "apply": bool(apply),
        "scopes": len(active),
        "merged": merged_total,
        "forgotten": len(forgotten),
        "seconds": round(time.time() - t0, 2),
        "classes": cls_counts,
        "budget": {"scopes": MAX_SCOPES, "forget": MAX_FORGET},
    }
    if apply:
        _ledger_add(row)
    return row


def stats() -> dict:
    from plugins import long_memory as lm
    total = sum(len(v) for v in lm._archive.values())
    return {"scopes": sum(1 for v in lm._archive.values() if v), "items": total,
            "forgotten_kept": len(_load_forgotten())}
