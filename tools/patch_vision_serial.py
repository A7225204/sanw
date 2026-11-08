import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

if "_LOCAL_VISION_LOCK" in s:
    print("已加过锁")
    raise SystemExit

# 1) 全局锁 + 包装函数：本地识图一次只跑一张，忙就直接走云端（绝不排队）
old_def = "async def _local_qwen_vision(image_bytes: bytes, prompt: str, timeout: int = 120) -> str:"
wrapper = '''_LOCAL_VISION_LOCK: asyncio.Lock | None = None


def _vision_lock() -> asyncio.Lock:
    global _LOCAL_VISION_LOCK
    if _LOCAL_VISION_LOCK is None:
        _LOCAL_VISION_LOCK = asyncio.Lock()
    return _LOCAL_VISION_LOCK


async def _local_qwen_vision(image_bytes: bytes, prompt: str, timeout: int = 120) -> str:
    """本地识图：全局串行，忙就跳过（不往 ComfyUI 队列里塞任务）。

    背景：以前每张图都提交一个任务，QwenVL 显存不够时全卡在队列里，
    越堆越多，主人在面板上看到一排"正在执行"。
    """
    try:
        await asyncio.wait_for(_vision_lock().acquire(), timeout=3)
    except Exception:  # noqa: BLE001
        logger.info("[识图] 本地正忙，跳过本次（不排队）")
        return ""
    try:
        # 队列里还有别人的活就不插队
        try:
            q = (await http_client.get(f"{COMFY_URL}/queue", timeout=8)).json()
            pending = len(q.get("queue_pending") or []) + len(q.get("queue_running") or [])
        except Exception:  # noqa: BLE001
            pending = 0
        if pending > 0:
            logger.info(f"[识图] ComfyUI 队列还有 {pending} 个任务，本次不排队")
            return ""
        return await _local_qwen_vision_inner(image_bytes, prompt, timeout)
    finally:
        try:
            _vision_lock().release()
        except Exception:  # noqa: BLE001
            pass


async def _local_qwen_vision_inner(image_bytes: bytes, prompt: str, timeout: int = 120) -> str:'''

if old_def in s:
    s = s.replace(old_def, wrapper, 1)
    p.write_text(s, encoding="utf-8")
    print("已加：全局锁 + 队列检查（一次一张，不排队）")
else:
    print("函数定义没找到")
