"""把视频真正投稿到 B 站（bilibili-api-python 的 VideoUploader）。

用法：
    python tools/bili_upload.py 视频.mp4 --title "标题" [--desc "..."] [--tid 21] \
        [--cover cover.jpg] [--tags 大肥鱼,AI语音] --yes

不带 --yes 只做登录校验（读 .bili_cookies.json 查自己账号），不会投稿。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COOKIE = ROOT / ".bili_cookies.json"


def _credential():
    from bilibili_api import Credential
    d = json.loads(COOKIE.read_text(encoding="utf-8"))
    if isinstance(d, dict) and isinstance(d.get("cookies"), dict):
        d = d["cookies"]
    return Credential(
        sessdata=d.get("SESSDATA") or "",
        bili_jct=d.get("bili_jct") or "",
        buvid3=d.get("buvid3") or "",
        dedeuserid=d.get("DedeUserID") or "",
    ), d


async def whoami(cred) -> str:
    from bilibili_api import user
    me = await user.get_self_info(cred)
    return f"{me.get('name')}（uid {me.get('mid')}）"


async def upload(video: Path, title: str, desc: str, tid: int,
                 tags: list[str], cover: Path | None) -> dict:
    from bilibili_api import video_uploader as vu
    cred, _ = _credential()
    page = vu.VideoUploaderPage(path=str(video), title=title)
    meta = {
        "act_reserve_create": 0,
        "copyright": 1,
        "desc": desc,
        "no_reprint": 0,
        "source": "",
        "tags": ",".join(tags)[:300],
        "tid": int(tid),
        "title": title[:80],
        "up_close_danmaku": False,
        "up_close_dynamic": False,
        "up_selection_reply": False,
    }
    uploader = vu.VideoUploader([page], meta, cred,
                                cover=str(cover) if cover and cover.exists() else "")
    return await uploader.start()


async def list_videos(cred, pn: int = 1, ps: int = 10) -> list[dict]:
    """列自己的投稿（member 端，含 aid/bvid/标题/状态）。"""
    import httpx
    d = json.loads(COOKIE.read_text(encoding="utf-8"))
    if isinstance(d, dict) and isinstance(d.get("cookies"), dict):
        d = d["cookies"]
    cookies = {k: v for k, v in d.items() if isinstance(v, str)}
    async with httpx.AsyncClient(timeout=20.0, cookies=cookies,
                                 headers={"User-Agent": "Mozilla/5.0",
                                          "Referer": "https://member.bilibili.com/"}) as c:
        r = await c.get("https://member.bilibili.com/x/web/archives",
                        params={"pn": pn, "ps": ps, "status": "is_pubing,pubed,not_pubed"})
    d2 = r.json()
    if d2.get("code") != 0:
        raise RuntimeError(f"列投稿失败：{d2.get('code')} {d2.get('message')}")
    data = d2.get("data") or {}
    return (data.get("arc_audits") or []) or (data.get("list") or {}).get("vlist") or []


async def delete_video(cred, aid: int) -> dict:
    """删稿（member 端，需要 csrf=bili_jct）。

    注意：B 站现在对删稿要**图形验证码**（返回 340022），纯 API 删不掉；
    这个函数保留是为了接口对齐，实际删稿要去创作中心网页点。
    """
    import httpx
    d = json.loads(COOKIE.read_text(encoding="utf-8"))
    if isinstance(d, dict) and isinstance(d.get("cookies"), dict):
        d = d["cookies"]
    cookies = {k: v for k, v in d.items() if isinstance(v, str)}
    csrf = cookies.get("bili_jct") or ""
    async with httpx.AsyncClient(timeout=30.0, cookies=cookies,
                                 headers={"User-Agent": "Mozilla/5.0",
                                          "Referer": "https://member.bilibili.com/"}) as c:
        r = await c.post("https://member.bilibili.com/x/web/archive/delete",
                         data={"aid": int(aid), "csrf": csrf})
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--desc", default="")
    ap.add_argument("--tid", type=int, default=21, help="分区 id，21=生活·日常")
    ap.add_argument("--tags", default="大肥鱼,AI语音")
    ap.add_argument("--cover")
    ap.add_argument("--yes", action="store_true", help="真的要投稿")
    ap.add_argument("--delete", type=int, help="删稿：传 aid")
    ap.add_argument("--list", action="store_true", help="列出自己最近的投稿")
    a = ap.parse_args()

    cred, _ = _credential()
    if a.list:
        rows = asyncio.run(list_videos(cred))
        for x in rows[:15]:
            arc = x.get("Archive") or x.get("archive") or x
            print(f'{arc.get("aid")}  {arc.get("bvid")}  {arc.get("title")}  '
                  f'state={arc.get("state")} {arc.get("state_desc") or ""}')
        return
    if a.delete:
        print("删稿结果：", json.dumps(asyncio.run(delete_video(cred, a.delete)),
                                     ensure_ascii=False)[:200])
        return
    v = Path(a.video)
    if not v.exists():
        raise SystemExit(f"找不到视频：{v}")

    async def run() -> None:
        try:
            who = await whoami(cred)
            print(f"登录账号：{who}")
        except Exception as e:  # noqa: BLE001
            print(f"(!) 登录校验失败：{type(e).__name__}: {e}")
            if a.yes:
                raise SystemExit("cookie 失效，先更新 .bili_cookies.json")
            return
        if not a.yes:
            print("（试运行）加 --yes 才会真正投稿")
            return
        res = await upload(v, a.title, a.desc, a.tid,
                           [t.strip() for t in a.tags.split(",") if t.strip()],
                           Path(a.cover) if a.cover else None)
        print("投稿结果：", json.dumps(res, ensure_ascii=False)[:300])

    asyncio.run(run())


if __name__ == "__main__":
    main()
