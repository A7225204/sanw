import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\jobs\starfield_layout.py")
s = p.read_text(encoding="utf-8")

old = '''def _semantic_vectors(nodes: list[dict]) -> dict[str, list[float]]:
    """从 memory/vec_index.json 取每条记忆的 bge-m3 向量（按文本 sha1 匹配）。"""
    idx = (_read_json(ROOT / "memory" / "vec_index.json").get("items") or {})
    out: dict[str, list[float]] = {}
    for n in nodes:
        if n.get("kind") != "memory":
            continue
        text = str(n.get("summary") or "")[:300]
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
        v = idx.get(key)
        if v:
            out[n["id"]] = v
    return out'''

new = '''def _semantic_vectors(nodes: list[dict]) -> dict[str, list[float]]:
    """从 memory/vec_index.json 取每条记忆的 bge-m3 向量。

    memory_vec 建索引时用的文本是 "subject：fact"（前 300 字），
    所以这里要用"主题名 + 事实"拼回去，单用 fact 会全部匹配失败。
    """
    idx = (_read_json(ROOT / "memory" / "vec_index.json").get("items") or {})
    by_id = {n["id"]: n for n in nodes}
    out: dict[str, list[float]] = {}
    for n in nodes:
        if n.get("kind") != "memory":
            continue
        fact = str(n.get("summary") or "")[:300]
        parent = by_id.get(str(n.get("parentId") or ""))
        subject = str((parent or {}).get("summary") or "").strip()
        cands = []
        if subject:
            cands.append(f"{subject}：{fact}"[:300])
        cands.append(fact)
        for text in cands:
            key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
            v = idx.get(key)
            if v:
                out[n["id"]] = v
                break
    return out'''

if "subject＝" in s or "主题名 + 事实" not in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("已修向量匹配（subject：fact）")
else:
    print("已修过")
