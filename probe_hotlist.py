"""一次性探针：测各公开热搜源在本机网络的可达性与标题字段。只打印状态码、条数、前 3 条标题。"""
import json
import httpx

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

sources = {
    "微博热搜": ("https://weibo.com/ajax/side/hotSearch", "weibo"),
    "百度热搜": ("https://top.baidu.com/api/board?platform=wise&tab=realtime", "baidu"),
    "知乎热榜": ("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=10", "zhihu"),
    "今日头条": ("https://www.toutiao.com/hot-event/hot-list/", "toutiao"),
}

for name, (url, kind) in sources.items():
    print("=" * 12, name, "=" * 12)
    try:
        r = httpx.get(url, headers=UA, timeout=12, follow_redirects=True)
        print("STATUS:", r.status_code, "bytes:", len(r.content))
        if r.status_code != 200:
            print("RAW:", r.text[:150]); continue
        data = r.json()
        titles = []
        if kind == "weibo":
            titles = [x.get("word", "") for x in (data.get("data", {}).get("realtime") or [])]
        elif kind == "baidu":
            cards = data.get("data", {}).get("cards") or []
            if cards:
                titles = [x.get("word", "") for x in cards[0].get("content", [])]
        elif kind == "zhihu":
            titles = [x.get("target", {}).get("title", "") for x in (data.get("data") or [])]
        elif kind == "toutiao":
            titles = [x.get("Title", "") for x in (data.get("data") or [])]
        titles = [t for t in titles if t][:5]
        print("条数:", len(titles))
        for t in titles[:3]:
            print("  -", t[:50])
    except Exception as e:
        print("异常:", type(e).__name__, str(e)[:150])
    print()
