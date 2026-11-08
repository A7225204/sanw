import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\feishu_bridge.py")
s = p.read_text(encoding="utf-8")

if "_upload_file_bytes" in s:
    print("已存在")
    raise SystemExit

helper = '''

def _upload_file_bytes(data: bytes, filename: str, file_type: str = "stream",
                       mime: str = "application/octet-stream") -> str:
    """通用上传：返回 file_key（语音用 file_type=opus，普通文件用 stream）。"""
    import httpx
    try:
        r = httpx.post(f"{_api_base()}/im/v1/files",
                       headers={"Authorization": f"Bearer {_token()}"},
                       data={"file_type": file_type, "file_name": filename},
                       files={"file": (filename, data, mime)},
                       timeout=120)
        key = ((r.json() or {}).get("data") or {}).get("file_key", "")
        if not key:
            logger.warning(f"[飞书] 传文件失败：{r.status_code} {r.text[:120]}")
        return key
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 传文件异常：{type(e).__name__}: {e}")
        return ""


def _send_image_file(path: str) -> bool:
    """本地图片文件 → 飞书图片消息。"""
    key = _upload_image(path)
    return bool(key and _send_by_type(OWNER_OPEN_ID, "image", {"image_key": key}))
'''

marker = "def _upload_bytes("
i = s.find(marker)
if i < 0:
    print("锚点没找到")
    raise SystemExit
s = s[:i] + helper.lstrip("\n") + "\n\n" + s[i:]
p.write_text(s, encoding="utf-8")
print("已加 _upload_file_bytes / _send_image_file")
