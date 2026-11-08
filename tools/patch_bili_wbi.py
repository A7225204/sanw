import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\bili_dl.py")
s = p.read_text(encoding="utf-8")

if "def _wbi_keys(" in s:
    print("已有 wbi")
    raise SystemExit

helper = '''

# ---------------------------------------------------------------- WBI 签名
# B站 /x/player/v2 已被 wbi/v2 取代：不带签名时返回的字幕会串到别的视频。
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _wbi_keys(jar: dict) -> tuple[str, str]:
    try:
        import httpx
        r = httpx.get("https://api.bilibili.com/x/web-interface/nav", timeout=15,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
                      cookies=jar or None).json()
        wbi = ((r.get("data") or {}).get("wbi_img") or {})
        img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        return img, sub
    except Exception:  # noqa: BLE001
        return "", ""


def _wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    import hashlib
    import time as _t
    raw = img_key + sub_key
    mixin = "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]
    p = dict(params)
    p["wts"] = int(_t.time())
    items = []
    for k in sorted(p):
        v = str(p[k])
        v = "".join(ch for ch in v if ch not in "!'()*")
        items.append(f"{k}={v}")
    query = "&".join(items)
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return p
'''

anchor = "def fetch_subtitles("
i = s.find(anchor)
s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# fetch_subtitles 改用 wbi/v2
s = s.replace(
    '''            p2 = cli.get("https://api.bilibili.com/x/player/v2",
                         params={"bvid": bvid, "cid": cid}).json()''',
    '''            img_k, sub_k = _wbi_keys(jar)
            params = {"bvid": bvid, "cid": cid}
            if img_k and sub_k:
                params = _wbi_sign(params, img_k, sub_k)
                api = "https://api.bilibili.com/x/player/wbi/v2"
            else:
                api = "https://api.bilibili.com/x/player/v2"
            p2 = cli.get(api, params=params).json()''')

p.write_text(s, encoding="utf-8")
print("bili_dl: 改用 wbi/v2 拉字幕")
