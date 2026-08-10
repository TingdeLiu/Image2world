# Image2World 本地 AI 推理后端

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
>
> 📐 实测这个限制有多严重：把场景按方位角切成 36 份，单帧重建**只有 6 份方向上存在几何**，
> 其余 30 个方向径向延伸为 0。想扩大覆盖，见下面的多视图重建。

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

## 🎥 多视图重建（视频 / 多张照片 → 单个高斯泼溅）

单图路径的天花板是几何只存在于那一个相机看到的地方。多视图路径不换模型、不训练、
不调 API：对走动视频的若干关键帧各跑一次 SHARP，用 COLMAP 求出的位姿把它们落到
同一个世界坐标系里再融合。

**为什么能这样接**：`unproject_gaussians` 本来就接受相机外参，单图路径传的是
`torch.eye(4)`，这才使得输出落在相机坐标系。换成真实的 world-to-camera 矩阵，
每一帧的高斯就自动落在同一个坐标系里。

### 安装

```bash
pip install pycolmap plyfile
```

> ⚠️ **PyPI 的 pycolmap wheel 不带 CUDA**（`pycolmap.has_cuda` 为 `False`），
> 所以特征提取和匹配都跑 CPU。代码里的 `SFM_MAX_IMAGE_SIZE=1600`、
> `SFM_MAX_FEATURES=4096` 就是按 CPU 成本选的，调高会显著变慢。
> 好处是不需要 CUDA Toolkit、不需要 MSVC、不需要单独装 COLMAP 二进制。

### 用法

两步，中间产物可复用（改关键帧数量重跑合并不必重做 SfM）：

```bash
# 1) 求相机位姿。source 可以是视频，也可以是一个按拍摄顺序命名的图片目录
python backend/tools/multiview_sfm.py <video-or-image-dir> --out .artifacts/mv --target-frames 200

# 2) 跑 SHARP 并合并成一个 PLY
python backend/tools/multiview_splat.py .artifacts/mv --out world.ply --keyframes 8
```

输出的 PLY 与单图路径**格式完全一致**（同样的顶点属性和 `intrinsic` / `extrinsic` /
`image_size` 等专有 element），所以 `createBinaryPlyLod`、`/api/splat-to-collider`
和 viewer 都不需要改动。世界坐标系锚定在第一个关键帧的相机系上，`flip_y`、地面标定
的语义因此和单图输出一致。

### 两个只有多视图才能做的修正

合并不是简单地把几帧堆在一起——堆起来会同时把每帧的错误也堆进去。有两步清理，
它们都依赖"存在第二个观测"这件事，**单图路径原理上做不到**：

**1. 倾斜校正（`--no-level` 可关）**

世界坐标系锚定在参考关键帧的相机上，而手持相机从来不是水平的，整个重建因此
继承了它的俯仰和滚转。实测某次走动是 **5°**，在 15 m 跨度的房间上意味着两端
**1.3 m** 的高差，把 17.7% 的点推到了"地板以下"。viewer 的 `groundPlaneOffset`
是一个平移，修不了旋转——只能在几何上改。

用两个独立估计求"上"方向：**相机轨迹平面**（人走路时相机高度近似恒定，轨迹的
最小方差方向就是竖直方向）和**地板平面 RANSAC**。实测两者夹角 0.65°，互相印证。
以轨迹为主，因为它的可靠性判据明确（走动是否覆盖了一个平面）；地板用于精化和
交叉验证。两者都不可靠时**不旋转**，而不是按猜测转。

> 判据上踩过的坑：直线走廊的轨迹奇异值是 `[大, ~0, ~0]`，只看"薄"会把它误判成
> 完美平面。必须同时要求"在第二个方向上有展开"。另外 RANSAC 在随机点云上也能凑出
> 11% 内点，而真实但杂乱的地板只有 16.5%——单看内点率区分不了，所以有轨迹佐证时
> 用低阈值，独自作证时要求 25%。

**2. 自由空间雕刻（`--no-carve` 可关）**

把每个高斯投影回所有关键帧：如果它落在某帧观测表面**前方**超过 `CARVE_MARGIN`，
那一帧就是在说"这里是空的"。两票即剔除。

> ⚠️ **这一步的收益有限，要有预期。** 实测剔除 15.7%（margin 0.35 m / 2 票），但
> 渲染上的改善并不明显。原因是它只能剔除位于观测表面**前方**的点；而"地板以下"
> 那类错误几何位于表面**后方**（被遮挡），不构成矛盾，剔不掉——那些主要靠倾斜校正
> 解决。至于贴着真实表面延伸的拉伸条纹，它们在深度上不与任何观测矛盾，这一步同样
> 无能为力，参见上面单视图重建的固有限制。
>
> 这**不是**之前试过并失败的几何过滤（按尺寸/各向异性剔除）。那个方法用高斯自身的
> 形状做判断，没有任何东西可以校验，结果连真实表面一起删了。这里判断的依据是其他
> 相机的独立观测。

### 拍摄要求（直接决定成败）

SfM 靠相邻帧的特征匹配，SIFT 在**旋转超过约 25° 后就不可靠**。

- **慢走**，不要原地快速转身（纯旋转还会因为没有视差导致三角化退化）
- 绕一圈回到起点，有助于闭环
- 避开大面积白墙、镜面、玻璃
- 30–60 秒足够

`--target-frames` 给少了会直接断链：实测把一段相邻间隔 4.4° 的序列抽稀 6 倍后，
23/59 个相邻对超过 25°，**注册率从 67% 掉到 12%**。注册率低于 60% 时命令会告警并
建议提高帧数——那不是可以忽略的提示，它意味着世界只覆盖了房间的一部分。

### 实测数据

Tanks and Temples `meetingroom`（371 帧，1960×1086，RTX 5060 Ti + CPU SfM），
3.5 cm 融合体素、含倾斜校正与自由空间雕刻：

| | 单帧 | **4 帧（默认）** | 6 帧 | 8 帧 |
|---|---|---|---|---|
| 高斯数 | 846,396 | 1,014,051 | 1,411,479 | 1,745,892 |
| PLY 体积 | 47.4 MB | **56.8 MB** | 79.0 MB | 97.8 MB |
| 可行走面积 | 42.4 m² | **193.8 m²** | 230.1 m² | 272.1 m² |
| 占据体积 | 127.5 m³ | 502.4 m³ | 638.4 m³ | 699.6 m³ |
| 有几何的方向 | 6/36 | **36/36** | 36/36 | 36/36 |
| 地板带高度 std | 0.234 m | 0.232 m | 0.190 m | 0.189 m |

**关键帧数是覆盖与清晰度的直接权衡**：4 帧就已经拿到全部 36/36 个方向的覆盖，
再加帧买的是面积（194 → 272 m²）和地板平整度，代价是每多一帧就多叠一层互相矛盾的
深度估计，画面更糊。默认取 4，因为清晰度是更稀缺的那个资源。

倾斜校正的效果（同一份数据，开关对比）：

| | 未校正 | 已校正 |
|---|---|---|
| 地板以下的点 | 21.7% | **0.4%** |
| collider 房间高度 | 5.35 m | 4.66 m |
| 估计出的倾斜 | — | 5.08°（4/6/8 帧完全一致） |

位姿精度（对照数据集自带的 ground truth，Sim(3) 对齐后）：相机位置残差中位数为轨迹
尺寸的 **0.337%**，朝向误差中位 **0.413°**、最大 1.463°。

耗时：371 帧 CPU SfM 约 20 分钟，8 帧 SHARP 推理加融合与清理约 3 分钟。

### 云雾感：根因与已排除的做法

合并后画面有一层"雾"。根因已经量化：**SHARP 各帧对同一表面的深度预测中位差
28.6 cm**（p75 77 cm，p90 133 cm），偏置只有 +0.8 cm——是随机分散，不是能靠全局
校正修掉的系统性偏移。8 帧把同一面墙预测到 8 个位置，叠起来就是雾。

以下做法**全部实测无效，不要重试**。判别力 =（剔除的噪声占比）÷（误伤的真实几何
占比），1.0x 表示完全分不清：

| 做法 | 判别力 |
|---|---|
| opacity 阈值 0.3 / 0.5 / 0.7 | 1.20x / 1.10x / 1.03x |
| 最长轴 > 0.05 m | 1.21x |
| 各向异性 > 6 | **0.92x**（比随机还差） |
| 孤立点 kNN 距离 p50–p90 | 1.13x – 1.29x |
| 多帧支持度 | 1.00x – 1.12x |
| 加粗融合体素（7 cm / 12 cm） | 只变稀疏，雾的结构不变 |

最能说明问题的是支持度：**82.5% 的高斯只被 1 帧支持，而它们的中位可见帧数是 3–4**
——其他帧看得见它，只是不同意它在哪。体素也救不了：体素下采样是"每格保留一个"的
选择而非平均，要让散布 28.6 cm 的估计落进同一格需要 ~30 cm 的体素，那时几何已经没了。

唯一对症但未验证的方向：SHARP 的 `forward(image, disparity_factor, depth)` 接受一张
深度图用于对齐预测，理论上可以两轮跑——第一轮合并出点云，投影回各帧得到共识深度，
第二轮喂回去强制对齐。但它自己的 docstring 建议推理时 `depth=None`，属于没有把握的实验。

> ⚠️ **代价**：full_res PLY 从 47 MB 涨到 57 MB（4 帧）。LOD 解决了浏览器加载，
> 但存储和传输成本是实打实的。`--keyframes` 和 `--voxel-size` 是最直接的两个旋钮。
>
> ⚠️ 度量尺度由 COLMAP 稀疏点与 SHARP 深度图做最小二乘得到。合成数据上误差 0.04%，
> 但真实数据上对应点比值的 IQR 达到中位数的 13.5%——中位数仍稳健，不过不要把它
> 当作毫米级标定。

回归测试（纯数学，不需要 GPU / 模型 / COLMAP）：

```bash
python -m pytest backend/test_multiview.py -q
```

### Splatfacto 联合优化原型（推荐的视频路线）

逐帧 SHARP 再合并会把每一帧彼此矛盾的深度一起保留下来；Splatfacto 则让所有视频帧
共同优化同一个 Gaussian 场景，直接针对重投影误差收敛。它现在已经接入创建世界弹窗；
图片继续使用 `/api/generate`，视频使用可恢复的 `/api/reconstruction-jobs` 任务接口：

```bash
python backend/tools/splatfacto_prototype.py <video-or-image-dir> \
  --out .artifacts/splatfacto-prototype
```

默认训练 5000 步，产物为 `<out>/export/splat.ply`，运行状态与耗时写入
`<out>/prototype_state.json`。脚本复用现有的抽帧与 COLMAP 位姿求解，随后在 WSL2 的
Nerfstudio/gsplat 环境中联合优化。失败后可以分别用 `--reuse-sfm` 和
`--reuse-training` 复用中间结果。

任务接口会把状态和日志持久化到 `.artifacts/reconstruction-jobs/<job-id>/`。可用质量档位为
`preview`、`standard`、`quality`；浏览器直接上传到 FastAPI，训练完成后由 Next.js 的
`/api/video-reconstruction/finalize` 打包 full-res/500k/150k/100k PLY、缩略图、碰撞网格和
`scene.json`。后端重启后未完成任务会标为 `interrupted`，可通过
`POST /api/reconstruction-jobs/<job-id>/resume` 从已有 SfM/检查点继续。

本机 RTX 5060 Ti 的依赖要求：PyTorch CUDA 12.8、CUDA Toolkit 12.8，以及 WSL2。
首次运行会为 Blackwell `sm_120` 编译 gsplat；脚本固定 `MAX_JOBS=1`，避免 nvcc
并行编译时把 WSL 内存打满。首次编译完成后会复用缓存。

仓库内 `docs/media/demo-loop.mp4` 是二次渲染的展示视频，并非原始相机素材，COLMAP
只能稳定注册 3/16 帧，因此仅用于验证全链路，不能代表算法画质上限。正式输入应把
注册率至少做到 60%，并尽量提供 30–90 秒、慢速移动、有视差且少反光的原始视频。

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
