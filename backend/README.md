# ImageWorld 本地 AI 推理后端

本目录包含基于 Python FastAPI 的后端，用于在你本地的 **RTX 5060 Ti 16GB** GPU 上运行开源模型（SAM 2、LaMa、TripoSR、Stable Audio Open 等）。

通过在本地完成这些重量级模型推理，我们绕开了昂贵的闭源 API，实现零生成成本，并让数据始终留在本地。

---

## 🛠️ 安装与环境配置

强烈建议使用 **Anaconda/Miniconda** 管理 Python 依赖。

### 第 1 步：创建 Conda 环境
打开终端（如 Anaconda Prompt）运行：
```bash
# 创建一个 Python 3.10 的新环境
conda create -n image2world python=3.10 -y
conda activate image2world
```

### 第 2 步：安装带 CUDA 的 PyTorch
为利用 RTX 5060 Ti GPU，安装带 CUDA 支持的 PyTorch：
```bash
# 安装带 CUDA 12.1 支持的 PyTorch / Torchvision / Torchaudio
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### 第 3 步：安装核心依赖
从 `requirements.txt` 安装主要依赖库：
```bash
pip install -r requirements.txt
```

### 第 4 步：安装 SAM 2（Segment Anything 2）
SAM 2 需要从 Meta 官方仓库安装：
```bash
# 克隆并安装 SAM 2
git clone https://github.com/facebookresearch/sam2.git
cd sam2
pip install -e .
cd ..
```

### 第 5 步：安装 TripoSR（stabilityai / VAST-AI-Research）
TripoSR 是用于秒级生成 3D 网格的引擎：
```bash
# 克隆并安装 TripoSR
git clone https://github.com/VAST-AI-Research/TripoSR.git
cd TripoSR
pip install -e .
cd ..
```

### 第 6 步：安装 SHARP（Apple 单图高斯泼溅重建）
SHARP 负责把修补后的干净背景板重建成可导航的 3D 高斯泼溅——也就是世界的"环境"本体。

> ⚠️ **不装它，生成会直接失败。** 早期版本会静默降级到本地房间模板——结果是"生成成功"但看到的是别人的房间，
> 所以现在改为报错退出（`background_failed`），暂存目录一并清理。
> 判断重建是否真实：`0-world-full_res.ply` 应有上百万个 splat，且 PLY 头里带
> `extrinsic` / `intrinsic` / `disparity` 等 SHARP 专有 element。

```bash
# 从仓库根目录执行；ml-sharp 与其他第三方仓库一样不入库，需自行克隆
git clone https://github.com/apple/ml-sharp.git backend/ml-sharp
cd backend/ml-sharp
pip install -e . --no-deps
cd ../..
```

**必须加 `--no-deps`**：`pyproject.toml` 把 `gsplat` 列为依赖，而 `gsplat` 需要
CUDA Toolkit + MSVC 从源码编译，Windows 上极难装。后端的推理路径
（`backend/server.py` 的 `get_sharp()`）刻意只走 `sharp.models` + `sharp.utils`，
绕开了 `sharp.cli` 里那个 gsplat 光栅化渲染器，**因此不需要 gsplat**。
其余依赖（torch、timm、plyfile）前面几步已经装好。

验证安装：
```bash
python -c "from sharp.models import create_predictor; from sharp.utils.gaussians import save_ply; print('SHARP OK')"
```

模型权重（约 1.5 GB）会在首次调用 `/api/image-to-splat` 时从 Apple CDN 自动下载。

> 🧱 **SHARP 只产出高斯,不产出碰撞体。** 生成管线会自动调用 `/api/splat-to-collider`
> 从点云推导一个碰撞代理并标定地面高度，依赖：`pip install scikit-image fast-simplification`。
> 缺依赖时该步骤会跳过并告警，不影响世界生成。
>
> ⚠️ **这个代理不是房间几何。** 单视角重建只覆盖相机看得见的表面——可见地板、正对相机的墙面、
> 物体正面；相机背后和遮挡物之后没有数据。角色走到可见区域边缘会直接穿出去，地面由
> `groundPlaneColliderEnabled` 的无限平面兜底。把它当作"看得见的障碍物"，不是封闭房间。
> 地面标定不受此限制影响，只要地板可见就准确。

### 第 7 步：下载模型权重
运行下载脚本自动获取 SAM 2 权重：
```bash
python download_models.py
```
*（TripoSR、LaMa、Stable Audio Open 等模型会在各自接口首次调用时从 Hugging Face Hub 自动下载。）*

### 第 8 步（可选）：安装 SAM 3 启用概念分割
SAM 2 的自动分割器产出的是**无标签**掩码（`object_0`、`object_1`…）。
**SAM 3** 支持概念/文本提示，能返回某个概念的所有实例，给出真实的**语义标签**（`bed`、`chair`、`backpack`…），并贯通到物体命名与 SFX 提示词。

> ✅ **已在本机实测可用**（RTX 5060 Ti 16 GB，torch 2.11.0+cu128，numpy 2.2.6）。下面是验证可行的步骤——注意 Windows 特有的几个坑。

```bash
# 8a. 克隆到 backend/（与 TripoSR 并列）
git clone https://github.com/facebookresearch/sam3.git backend/sam3

# 8b. 用 --no-deps 安装，避免 sam3 把你环境里可用的 numpy 2.x / torch 降级
#     （sam3 锁定 numpy<2，但实测在 numpy 2.x 下也能跑）
pip install -e ./backend/sam3 --no-deps

# 8c. 补装它实际需要的运行依赖（这些不会动 numpy/torch）
pip install "timm>=1.0.17" "ftfy==6.1.1" regex "iopath>=0.1.10" pycocotools

# 8d. 仅 Windows：SAM 3 的 EDT kernel 硬依赖 `triton`，官方无 Windows 版，
#     使用社区移植版：
pip install triton-windows
```

**权重（gated 受限）：** 先到 <https://huggingface.co/facebook/sam3> 申请访问，再执行 `huggingface-cli login`。image 模型会拉取 `config.json` + `sam3.pt`（约 3.45 GB）。

> ⚠️ **务必直连 huggingface.co 下载——不要给这些权重设置 `HF_ENDPOINT` 镜像（如 hf-mirror.com）。** 镜像不服务 gated 的 `facebook/sam3` 仓库，会报 `LocalEntryNotFoundError`。本机 huggingface.co 可直连，保持 `HF_ENDPOINT` 不设即可。首次运行前可预先缓存：
> ```python
> from huggingface_hub import hf_hub_download
> hf_hub_download("facebook/sam3", "config.json")
> hf_hub_download("facebook/sam3", "sam3.pt")   # 约 3.45 GB
> ```

> ℹ️ `server.py` 已自动处理两件事：通过 `sam3.__path__` 解析 BPE 词表（规避 editable 安装下 `__file__=None` 的问题）；并在 `torch.autocast(bfloat16)` + `inference_mode` 上下文内执行推理（必须——SAM 3 是 bf16，否则会报 dtype 不匹配）。

> 📜 权重采用 Meta 的**自定义 SAM License**（允许商用但有限制）——SaaS 化前请审阅条款。

---

## 🔀 分割后端（SAM 2 对比 SAM 3）

`/api/segment` 同时支持两种后端，并具备优雅降级：

- **默认 = SAM 2**（无标签自动掩码），无需任何改动。
- **切换到 SAM 3**：设置环境变量（后端默认值与 Next.js 管线路由都会读取它）：
  ```bash
  # PowerShell:  $env:IMAGEWORLD_SEGMENTER = "sam3"
  # bash:        export IMAGEWORLD_SEGMENTER=sam3
  ```
  Next.js 的 `/api/generate` 路由会把它作为 `segmenter` 表单字段转发。你也可以直接向 `/api/segment` POST `segmenter=sam3`（以及可选的逗号分隔 `concepts` 列表）。
- 若请求 `sam3` 但未安装/无权重，后端会**记录告警并回退 SAM 2**，管线绝不硬失败。响应中包含 `segmenter_used`，便于确认实际使用的是哪个后端。

---

## 🚀 启动服务

启动本地推理服务：
```bash
python server.py
```
服务将运行在 **`http://localhost:8000`**。

---

## 💡 16GB 显存下的 VRAM 与 GPU 优化

为避免 CUDA 显存溢出（OOM），同时让你能并行编辑、渲染、浏览 R3F WebGL 场景：
1. **延迟加载**：模型不在启动时加载进显存，仅在对应接口被调用时按需加载。
2. **动态卸载**：每次推理完成后，临时卸载模型权重并调用 `torch.cuda.empty_cache()` 立即释放显存。
3. **TripoSR 高效**：TripoSR 在 5060 Ti 上 `< 1.0` 秒即可完成，仅占用 `~3GB` 显存，是腾讯 Hunyuan3D-2.1 的极轻量替代方案。
