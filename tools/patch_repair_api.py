import pathlib

# ---------- 1) trauma_mem：修复计划（一键 / 快速） ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\trauma_mem.py")
s = p.read_text(encoding="utf-8")

if "def start_repair" not in s:
    add = '''

def _now_ts() -> float:
    return time.time()


def effective_charge(it: dict) -> float:
    """带修复计划的电荷：快速修复期间线性下降，完成后归零。"""
    charge = float(it.get("charge") or 0)
    plan = it.get("repair_plan") or {}
    if not plan:
        return charge
    try:
        started = float(plan.get("started") or 0)
        dur = float(plan.get("duration") or 0)
    except (TypeError, ValueError):
        return charge
    if dur <= 0:
        return charge
    prog = max(0.0, min(1.0, (_now_ts() - started) / dur))
    from_charge = float(plan.get("from_charge") or charge)
    return max(0.0, from_charge * (1.0 - prog))


def start_repair(scope: str, subject: str, mode: str = "quick",
                 duration_min: float = 10.0) -> dict | None:
    """一键修复（instant，立即归零）/ 快速修复（quick，按时间线性掉）。"""
    for it in _load()["items"]:
        if it.get("scope") != scope or it.get("subject") != subject:
            continue
        now = _now_ts()
        if mode == "instant":
            it["charge"] = 0.0
            it["repair"] = round(float(it.get("repair") or 0) + 1.0, 3)
            it["repair_plan"] = None
            it["repaired_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            dur = max(1.0, float(duration_min)) * 60.0
            it["repair_plan"] = {
                "started": now,
                "duration": dur,
                "from_charge": effective_charge(it),
            }
        it["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save()
        return {
            "scope": scope, "subject": subject, "mode": mode,
            "charge": round(effective_charge(it), 4),
            "remainingSec": int(max(0.0, (float((it.get("repair_plan") or {}).get("started") or 0)
                                     + float((it.get("repair_plan") or {}).get("duration") or 0)
                                     - now))) if it.get("repair_plan") else 0,
        }
    return None


def repair_status(it: dict) -> dict:
    plan = it.get("repair_plan") or {}
    remain = 0
    progress = 0.0
    if plan:
        try:
            start = float(plan.get("started") or 0)
            dur = float(plan.get("duration") or 0)
            progress = max(0.0, min(1.0, (_now_ts() - start) / dur)) if dur > 0 else 0.0
            remain = int(max(0.0, start + dur - _now_ts()))
        except (TypeError, ValueError):
            pass
    return {"charge": round(effective_charge(it), 4), "progress": round(progress, 3),
            "remainingSec": remain, "repairing": bool(plan and remain > 0)}
'''
    s = s.rstrip() + add
    p.write_text(s, encoding="utf-8")
    print("trauma_mem: 修复计划（instant / quick）")

# ---------- 2) 后端接口：POST /api/memory/node/<id>/repair ----------
d = pathlib.Path(r"D:\qq-ai-bot\dashboard\server.py")
u = d.read_text(encoding="utf-8")
if "/repair" not in u:
    u = u.replace('''        m = _re.match(r"^/api/memory/node/([^/]+)/merge$", path)''',
'''        m = _re.match(r"^/api/memory/node/([^/]+)/repair$", path)
        if m and method == "POST":
            payload = self._read_body_json()
            node_id = m.group(1)
            parts = node_id.split(":")
            subject = str(payload.get("subject") or "")
            if len(parts) >= 2 and not subject:
                # 节点 id 里的 scope 已知，subject 从创伤库反查
                try:
                    import json as _j
                    from pathlib import Path as _P
                    data = _j.loads((_P(r"D:\\qq-ai-bot") / "memory" / "trauma.json").read_text(encoding="utf-8-sig"))
                    for it in data.get("items") or []:
                        if str(it.get("scope")) == parts[1]:
                            subject = str(it.get("subject") or "")
                            break
                except Exception:  # noqa: BLE001
                    subject = ""
            import importlib.util as _iu
            spec = _iu.spec_from_file_location("trauma_mem", r"D:\\qq-ai-bot\\plugins\\trauma_mem.py")
            mod = _iu.module_from_spec(spec); spec.loader.exec_module(mod)  # type: ignore[union-attr]
            res = mod.start_repair(parts[1] if len(parts) >= 2 else "", subject,
                                   str(payload.get("mode") or "quick"),
                                   float(payload.get("minutes") or 10))
            self._send_json(res or {"ok": False, "error": "没找到对应的创伤条目"})
            return True
        m = _re.match(r"^/api/memory/node/([^/]+)/merge$", path)''', 1)
    d.write_text(u, encoding="utf-8")
    print("dashboard: /repair 路由")

# ---------- 3) detail 里带修复状态 ----------
m = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz.py")
v = m.read_text(encoding="utf-8")
if "repairStatus" not in v:
    v = v.replace('''                "trauma": trauma.get(str(it.get("subject") or "").strip()),''',
'''                "trauma": trauma.get(str(it.get("subject") or "").strip()),
                "_trauma_subject": str(it.get("subject") or "").strip(),''', 1)
    v = v.replace('''                return {
                    "node": me or {"id": node_id, "scope": scope},''',
'''                _tr = None
                try:
                    import importlib.util as _iu2
                    _spec = _iu2.spec_from_file_location("trauma_mem", ROOT / "plugins" / "trauma_mem.py")
                    _mod = _iu2.module_from_spec(_spec); _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
                    _item = _mod.by_subject(scope, str((me or {}).get("_trauma_subject") or ""))
                    if _item:
                        _tr = {**_mod.repair_status(_item), "triggers": _item.get("triggers") or [],
                               "since": _item.get("since") or ""}
                except Exception:  # noqa: BLE001
                    _tr = None
                return {
                    "node": me or {"id": node_id, "scope": scope},
                    "trauma": _tr,''', 1)
    m.write_text(v, encoding="utf-8")
    print("memory_viz: detail 带修复状态")
