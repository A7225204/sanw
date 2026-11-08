import httpx, json
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
r = httpx.get("https://top.baidu.com/api/board?platform=wise&tab=realtime", headers=UA, timeout=12)
d = r.json()
c = d["data"]["cards"][0]
print("component:", c.get("component"))
cont = c["content"]
print("content len:", len(cont))
print(json.dumps(cont[0], ensure_ascii=False)[:800])
print("---- all titles ----")
for x in cont[:8]:
    print(json.dumps({k: x.get(k) for k in ("word", "query", "title", "hotScore", "desc")}, ensure_ascii=False))
