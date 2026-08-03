# Image2World 使用说明

把**单张图片**变成可漫游的 3D 世界：SHARP 生成真实背景高斯溅射 + 你挑选的物体（SAM 分割 → TripoSR 出 3D 网格）+ AudioLDM 碰撞音效。本文是日常操作手册。安装见 [backend/README.md](./backend/README.md)。

---

## 1. 启动（开两个终端）

### 终端 ①：AI 推理后端（FastAPI，端口 8000）
```powershell
conda activate image2world
python backend\server.py
```
- 看到 `Uvicorn running on http://0.0.0.0:8000` 即就绪。
- 模型**延迟加载**：首次调用某功能才加载对应模型（SHARP 权重已缓存，无需重下）。
- 交互式接口文档：<http://localhost:8000/docs>

> 可选：想让**自动模式**也用 SAM 3 语义分割（默认自动模式用 SAM 2），先设环境变量再启动：
> ```powershell
> $env:IMAGEWORLD_SEGMENTER = "sam3"
> ```
> （注意：下面「指定类别」和「点击」两种方式**与此开关无关**，始终各自用 SAM 3 / SAM 2。）

### 终端 ②：前端（Next.js，端口 3000）
```powershell
npm run dev
```

前端会自动检测 `http://localhost:8000` 的后端状态。使用其他地址时，在启动前端前设置
`IMAGEWORLD_BACKEND_URL`；生成失败的任务只会保留在隐藏暂存目录并自动清理，不会出现在世界列表。
打开 <http://localhost:3000>，默认跳到第一个场景。

---

## 2. 生成一个世界

左侧边栏点 **Create New World**，弹窗里：

1. **World Name**：起个名字。
2. **Source Image**：拖入或点击上传一张室内照片。
3. **选哪些物体做成 3D**（三选一，可留空）：

   | 方式 | 操作 | 特点 |
   | :--- | :--- | :--- |
   | **自动** | 三项都不填 | 自动挑面积最大的前 5 个物体 |
   | **指定类别** | 在 *Objects to Extract* 填 `chair, monitor, lamp`（逗号分隔） | SAM 3 概念分割，物体带**语义名**（`chair-0`…） |
   | **点击图像** | 选图后下方出现大图，**点哪个物体抠哪个**，缩略图条显示已选，点 `×` 移除 | SAM 2 点提示，最精确；物体名为 `object_0…` |

   > 优先级：**点击 > 类别 > 自动**。三者都会接 TripoSR 出物体、SHARP 出背景。

4. 点 **Generate World**，等待推理：
   - 首次约 1~3 分钟（含模型加载）；之后约 30 秒~2 分钟，取决于物体数量。
   - 完成后自动跳转到新世界。

生成产物在 `public/worlds/<slug>/`：背景 `output/world/0-world-full_res.ply`（SHARP）、各物体 `output/<物体>/*.glb`、音效 `*/sfx/*.wav`、布局 `scene.json`。

---

## 3. 在 3D 世界里

- **漫游**：`W`/`A`/`S`/`D` 移动，`Space` 跳跃，鼠标转视角。
- **物理 + 音效**：撞到物体会触发对应的碰撞音效。
- **编辑摆放**：点边栏铅笔图标进编辑器，选中物体平移/旋转，保存写回 `scene.json`。
- **重置**：边栏 Reset 图标把物体复位。

---

## 4. 背景说明（SHARP）

背景是用你上传图的 **clean plate**（已抠掉前景物体）经 Apple SHARP 单图重建的真实 3D 高斯溅射——是**你那张照片的房间**，可在原视角附近漫游、有真实纵深。

> 这是单目重建，**不是完整 360° 房间**：背离原视角太远会露馅，属模型固有限制。
> 若 SHARP 不可用，会自动降级为复制 `home-room` 静态背景（管线不中断）。

---

## 5. 常见问题

- **物体几何很粗糙、看不清**：这是 TripoSR 模型的质量上限（换它需要更重的模型如 TRELLIS/Hunyuan3D，属后续计划）。指定类别/点击能保证**抠对东西**，但提升不了几何清晰度。
- **生成失败 / 某步报错**：单步失败会跳过并继续（如某物体 3D 失败不影响其他）。查后端终端日志定位。
- **显存紧张（16GB）**：SHARP + SAM 3 + TripoSR + AudioLDM 顺序加载。若 OOM，重启后端清显存，或减少一次生成的物体数量。
- **首次很慢**：模型在首次调用时从网络下载并缓存（SHARP 权重 2.8GB 已预下；TripoSR/AudioLDM 首次会拉取）。
- **改了后端代码**：需重启终端 ① 的 `server.py` 生效；前端 `npm run dev` 会热更新。
