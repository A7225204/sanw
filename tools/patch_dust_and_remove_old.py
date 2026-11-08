import pathlib
import re

# 1) 星尘点加光晕贴图，避免远处出现小方块
p = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\StarfieldView\starfieldRenderer.ts")
s = p.read_text(encoding="utf-8")
s = s.replace(
"""  const dustMat = new THREE.PointsMaterial({
    color: 0x86c8ff, size: 0.012, transparent: true, opacity: 0.55,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });""",
"""  const dustTex = makeGlowTexture();
  const dustMat = new THREE.PointsMaterial({
    color: 0x86c8ff, size: 0.03, transparent: true, opacity: 0.5,
    blending: THREE.AdditiveBlending, depthWrite: false, map: dustTex,
    sizeAttenuation: true,
  });""", 1)
p.write_text(s, encoding="utf-8")
print("星尘已改成光晕贴图")

# 2) 主面板移除旧的「记忆星河」（按钮 + 覆盖层 + 脚本）
h = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
html = h.read_text(encoding="utf-8")
before = len(html)

html = html.replace('<button id="memBtn" class="mem-btn" title="查看记忆星系">🕸 记忆</button>\n    ', "", 1)

start = html.find('<div id="memOverlay"')
if start >= 0:
    end = html.find("</script>", start)
    if end >= 0:
        end += len("</script>")
        html = html[:start] + html[end:]
        print("已删除旧记忆星河覆盖层")

# 清掉只在旧星河用到的样式块（保留 .mem-btn 给可视化入口用）
for cls in (".mem-overlay", ".mem-head", ".mem-title", ".mem-stats", ".mem-close", "#memCanvas",
            ".mem-legend", ".mem-tip", ".mem-hint", ".mem-left", ".mem-right", ".mem-sec",
            ".mem-row", ".mem-item", ".mem-close2"):
    html = re.sub(rf"\n\s*{re.escape(cls)}[^{{]*\{{[^}}]*\}}", "", html)

h.write_text(html, encoding="utf-8")
print(f"index.html: {before} → {len(html)} 字节")
