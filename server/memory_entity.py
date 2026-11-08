"""实体树：按「分类」而不是「会话」组织的二维树。

主人 2026-10-31 定的结构（默认展开到第二层）：

  蓝色大肥鱼
  ├── 🧬 人格与规则
  ├── 👤 人物卡
  ├── 📚 长期记忆
  ├── 🎬 经历与事件（带 [CORT高] / [DA高] 标记）
  ├── 💭 念头池
  └── 🌙 梦境区

节点 id 尽量复用现有体系的真实 id（growth:… / cat:card:… / mem:… / ep:…），
这样点开节点时右侧详情不用另写一套解析。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(rel: str) -> dict:
    try:
        d = json.loads((ROOT / rel).read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _node(nid: str, summary: str, parent: str | None, *,
          kind: str = "memory", scope: str = "", t: int = 0,
          retention: float = 0.6, category: str = "", extra: dict | None = None) -> dict:
    n = {
        "id": nid, "scope": scope, "parentId": parent, "topicId": None,
        "summary": summary, "sourceMessageIds": [], "kind": kind,
        "retentionScore": retention, "importance": 0.6, "recency": 0.6,
        "frequency": 0.3, "createdAt": "", "lastAccessedAt": "",
        "category": category, "t": t,
    }
    if extra:
        n.update(extra)
    return n


def entity_tree(scope: str = "") -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []

    root_id = "persona:me"
    nodes.append(_node(root_id, "蓝色大肥鱼", None, kind="scope", scope=scope, retention=1.0))

    def branch(bid: str, title: str) -> None:
        nodes.append(_node(bid, title, root_id, kind="topic", scope=scope, retention=1.0))
        edges.append({"source": root_id, "target": bid, "kind": "hierarchy"})

    def child(nid: str, title: str, bid: str, **kw) -> None:
        nodes.append(_node(nid, title, bid, scope=scope, **kw))
        edges.append({"source": bid, "target": nid, "kind": "hierarchy"})

    # ── 1. 人格与规则 ────────────────────────────────────────────────
    branch("entity:persona", "🧬 人格与规则")
    try:
        from server import memory_growth as mg
        for it in mg.list_items():
            st = str(it.get("status") or "pending")
            if st not in ("solidified", "pending"):
                continue
            mark = "✅" if st == "solidified" else "🟡"
            child(str(it.get("id")), f"{mark} {it.get('trait')}：{str(it.get('value'))[:16]}",
                  "entity:persona", t=int(it.get("t") or 0), retention=1.0 if st == "solidified" else 0.85,
                  extra={"status": st, "trait": it.get("trait")})
    except Exception:  # noqa: BLE001
        pass

    # ── 2. 人物卡 ────────────────────────────────────────────────────
    branch("entity:characters", "👤 人物卡")
    try:
        from server import memory_viz_persona as mvp
        cards = mvp._read_json(ROOT / "memory" / "character_cards.json")
        rel = mvp._read_json(ROOT / "memory" / "relation_state.json")
        # 昵称表：跨群取 seen 最多的那个当显示名
        nick_names: dict[str, str] = {}
        nick_seen: dict[str, int] = {}
        for _g, people in (mvp._read_json(ROOT / "memory" / "nick_names.json") or {}).items():
            if not isinstance(people, dict):
                continue
            for uid, rec in people.items():
                if not isinstance(rec, dict):
                    continue
                nm = str(rec.get("current") or rec.get("first") or "").strip()
                seen = int(rec.get("seen") or 0)
                if nm and seen >= nick_seen.get(str(uid), -1):
                    nick_names[str(uid)] = nm
                    nick_seen[str(uid)] = seen
        rows = sorted((rel or {}).items(), key=lambda kv: -float((kv[1] or {}).get("last") or 0))[:40]
        for uid, st in rows:
            card = cards.get(str(uid)) or {}
            name = card.get("nickname") or nick_names.get(str(uid)) or f"QQ {uid}"
            sub = card.get("性格") or ""
            child(f"cat:card:{uid}", f"{name}{('·' + sub[:8]) if sub else ''}",
                  "entity:characters", t=mvp._to_ms(st.get("last")))
    except Exception:  # noqa: BLE001
        pass

    # ── 3. 长期记忆 ──────────────────────────────────────────────────
    branch("entity:long", "📚 长期记忆")
    try:
        arch = (_load("memory/长期记忆.json").get("archive") or {})
        rows = []
        for sc, lst in arch.items():
            for it in lst or []:
                if isinstance(it, dict):
                    rows.append((sc, it))
        rows.sort(key=lambda x: -float(x[1].get("lu") or 0))
        for sc, it in rows[:24]:
            child(f"mem:{sc}:{it.get('id')}", f"{it.get('subject') or '群友'}：{str(it.get('fact'))[:16]}",
                  "entity:long", t=int(float(it.get("lu") or 0) * 1000),
                  retention=round(min(1.0, float(it.get("w") or 1) / 3.0), 3))
    except Exception:  # noqa: BLE001
        pass

    # ── 4. 经历与事件（带内分泌标记）────────────────────────────────
    branch("entity:episodes", "🎬 经历与事件")
    try:
        for it in (_load("memory/episodes.json").get("items") or [])[:20]:
            if not isinstance(it, dict):
                continue
            child(str(it.get("id")), f"🎬 {str(it.get('title'))[:18]}", "entity:episodes",
                  t=int(time.time() * 1000), extra={"mood": it.get("mood")})
        for i, it in enumerate((_load("memory/trauma.json").get("items") or [])[:20]):
            if not isinstance(it, dict):
                continue
            charge = float(it.get("charge") or 0)
            mark = "⚠️CORT高" if charge >= 0.5 else "CORT中"
            child(f"trauma:{i}", f"[{mark}] {str(it.get('subject'))[:14]}",
                  "entity:episodes", retention=0.95)
    except Exception:  # noqa: BLE001
        pass

    # ── 5. 念头池 ────────────────────────────────────────────────────
    # 2026-11-05：原来标题固定「💭 念头池」且只取最近 20 条 —— 点进去看到的是
    # 一个笼统的分类节点，条数看不出来，超过 20 条的直接消失。
    # 现在标题带总数，且**全部列出**（念头池本来就不该截断，它是"她此刻在想什么"）。
    try:
        raw = _load("memory/thoughts.json").get("items") or []
        now = time.time()
        alive = [x for x in raw
                 if isinstance(x, dict) and float(x.get("created") or 0) > now - 86400 * 7]
        alive.sort(key=lambda x: -float(x.get("created") or 0))
        branch("entity:thoughts", f"💭 念头池（{len(alive)} 条）")
        for i, it in enumerate(alive):
            _t = str(it.get("text") or "").strip()
            _ts = time.strftime("%m-%d %H:%M", time.localtime(float(it.get("created") or 0)))
            # 2026-11-07：带上 id / why / poked / src / scope —— 面板点一条念头时
            # 原来只能看到 40 字摘要，问不到「这句是谁说的、什么时候说的」。
            child(f"thought:{it.get('id') or i}", f"💭 {_t[:40]}｜{_ts}", "entity:thoughts",
                  retention=1.0,
                  kind="thought",
                  extra={"thought": {
                      "id": it.get("id"),
                      "text": _t,
                      "why": it.get("why") or "",
                      "priority": it.get("priority"),
                      "poked": int(it.get("poked") or 0),
                      "created": it.get("created"),
                      "src": str(it.get("src") or ""),
                      "scope": str(it.get("scope") or ""),
                  }})
    except Exception:  # noqa: BLE001
        branch("entity:thoughts", "💭 念头池")

    # ── 6. 梦境区 ────────────────────────────────────────────────────
    branch("entity:dreams", "🌙 梦境区")
    try:
        for i, it in enumerate((_load("memory/dreams.json").get("items") or [])[-10:]):
            if not isinstance(it, dict):
                continue
            child(f"dream:{i}", f"🌙 {str(it.get('at'))[:10]} {str(it.get('dream'))[:16]}",
                  "entity:dreams")
    except Exception:  # noqa: BLE001
        pass

    return {
        "scope": scope,
        "nodes": nodes,
        "edges": edges,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
