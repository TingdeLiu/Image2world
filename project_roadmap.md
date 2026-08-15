# Image2World 项目商业化规划与技术路线图 (Roadmap)

本项目致力于将单张图片转化为交互式 3D 空间与物体的管线（参考 `image-blaster`），并通过**开源模型自部署**替代昂贵的商业 API（如 Fal.ai、World Labs、ElevenLabs），最终实现低成本、高毛利的商业化盈利。

---

## 1. 商业化盈利模式与核心壁垒

1. **SaaS 订阅与算力点数 (Credits)**：
   - 基础版 / 专业版 / 团队版订阅，每月赠送固定算力点数。
   - 超额点数加油包。
2. **创作者与开发者 API 服务**：
   - 提供标准 RESTful API / Webhook，让游戏开发者、独立站电商、建筑设计师能批量将 2D 资产转化为 3D Splat/GLB 场景。
3. **私有化部署 (Enterprise Edition)**：
   - 针对有高安全性需求的企业（如设计院、保密游戏工作室），提供整套自部署方案。
4. **核心壁垒**：
   - **全自动管线**：一键生成「背景 Splat + 前景物理物体 + 空间音效 + 碰撞网格」的完整世界。
   - **极致的渲染体验**：基于 WebGL/WebGPU 的超顺滑高斯溅射和物理碰撞体验。
   - **极低的推理成本**：通过自部署开源模型和动态 GPU 调度，将单次生成成本降至商业 API 的 10% 以下。

---

## 2. 核心技术栈演进（从 API 到开源自部署）

| 模块 | 原版 `image-blaster` 方案 | 本地开源自部署方案 (推荐) | 开源方案优势与部署要求 |
| :--- | :--- | :--- | :--- |
| **世界高斯溅射** | World Labs `marble-1.1` (高昂) | **TripoSplat** 或 **LGM** (Large Multi-View Gaussian Model) | **LGM / TripoSplat** 支持单图直接生成 3D 高斯溅射 (`.ply` / `.splat` / `.spz`)。<br>部署：PyTorch + CUDA，推荐 16GB+ VRAM GPU (如 RTX 3090/4090)。 |
| **3D 物体生成** | Hunyuan3D-v3 / Meshy (Fal.ai) | **Hunyuan3D-2.1** (Tencent) | 腾讯最新开源的 Hunyuan3D-2.1 效果极佳，包含几何生成 (DiT) 和 PBR 材质合成 (Paint)。<br>部署：VRAM 24GB，支持 Apache 2.0 商业许可。 |
| **物体分割与清理** | `nano-banana` / `gpt-image-2` | **SAM 2** (Segment Anything 2) + **LaMa** (Inpainting) 或 **FLUX.1 Fill** | **SAM 2** 负责超精确的物体轮廓分割，**LaMa / FLUX Fill** 负责擦除物体生成干净背景（Clean Plate）。<br>部署：LaMa 非常轻量，CPU 即可运行；SAM 2 与 FLUX 需要 GPU。 |
| **音效与环境音** | ElevenLabs SFX (按字数收费极贵) | **Stable Audio Open 1.0** 或 Meta **AudioGen (AudioCraft)** | Stable Audio Open 适合生成长达 47 秒的氛围音和 Foley 音效，AudioGen 适合生成短的物体撞击/交互音效。<br>部署：PyTorch，单张卡即可轻松运行。 |
| **Web 渲染端** | React + Three.js + Radix UI | **Next.js + Three.js / R3F + Tailwind CSS** | 引入 Next.js 方便做 SSR、SEO、用户登录态校验和商业支付路由。 |
| **后端与队列** | 无后端 (磁盘优先 CLI) | **FastAPI (Python) + Celery / Redis 队列** | 商业化必须引入后端处理高并发的 GPU 推理任务排队，并在生成完毕后通过 Webhook 推送给前端。 |
| **存储介质** | 本地 `worlds/` 目录 | **Cloudflare R2** / AWS S3 + 数据库 (PostgreSQL) | R2 零流出流量费 (No Egress Fees)，极度适合分发大体积的高斯溅射 `.spz` 和 GLB 模型。 |

---

## 3. Agent Team 协作开发规划

为了提高开发效率，我们将基于 `/teamwork-preview` 和 Subagent 系统，构建一个由 4 个专业 Agent 组成的开发团队：

```mermaid
graph TD
    A[Project Coordinator / Lead Agent] --> B[AI Inference Engineer]
    A --> C[Fullstack Developer]
    A --> D[3D/Web3D Front-end Engineer]
    
    B -->|提供 AI API 端点| C
    C -->|提供业务 API 与数据流| D
    D -->|集成 3D 渲染与交互| A
```

### 1) Lead Agent (项目主协调)
- **职责**：任务分发，进度跟进，把控接口规范与系统集成。
- **目标**：保证生成管线与 Web 应用在每一步都完美对接，避免接口冲突。

### 2) AI Inference Engineer (算法部署 Agent)
- **职责**：负责在 GPU 环境（如 Linux/AutoDL/RunPod）上部署 SAM 2, LaMa, Hunyuan3D-2.1, LGM 和 Stable Audio Open。
- **任务**：编写 Python FastAPI 封装的微服务接口，将模型封装为统一的 API，接收图片并返回生成的 3D 网格/溅射/音频，保证推理速度与并发控制。

### 3) Fullstack Developer (全栈业务 Agent)
- **职责**：构建 Next.js 商业化应用后端，设计数据库模型。
- **任务**：集成 Clerk/NextAuth 登录、Stripe/微信/支付宝支付、算力消费点数扣减、Cloudflare R2 资产上传、异步推理任务队列管理（Redis/Celery）。

### 4) 3D/Web3D Front-end Engineer (前端 3D 交互 Agent)
- **职责**：重构并优化 React Three Fiber 3D 场景渲染和摆放编辑器。
- **任务**：实现顺畅的 SPZ Splat 渲染，整合 Rapier 物理引擎，优化第一人称操控、空间 3D 音频播放，以及精美的 SaaS UI 界面。

---

## 4. 落地实施步骤 (Step-by-Step Implementation)

### 阶段一：原型迁移与架构搭建 (Weeks 1-2)
- [x] **初始化项目**：在 `image2world` 中搭建 Next.js 15 + Tailwind CSS 的基本架构。
- [x] **迁移 viewer 代码**：将 `image-blaster/app` 中成熟的 Three.js / R3F 渲染和摆放编辑器（PlacementEditor）代码移植进来，使用本地 JSON 加载模式。
- [x] **接口对接设计**：开发了 Next.js 服务器 API 接口，实现 `/api/worlds` 扫描与 `/api/scene-project` 读写，打通了前后端对接。

### 阶段二：本地开源 AI 推理节点部署 (Weeks 3-4)
- [x] **初始化 AI 推理微服务框架**：搭建了 Python FastAPI + Uvicorn 架构的 `backend/server.py`，设计了延迟加载与 VRAM 清理机制以适配 5060 Ti 16GB 显存。
- [x] **并发保护**：所有重端点改为经 `asyncio.to_thread` + `Semaphore(1)` 调度——推理不再阻塞事件循环（健康检查 9.04 s → 0.21 s，此前会导致前端 3 秒超时误报「后端离线」），并发请求排队而非争抢显存（实测 3 并发完美串行）。详见 PROJECT_REPORT §3.12。
- [x] **图像预处理与 Clean Plate**：已实现 `/api/segment`（SAM 2 自动掩码 + base64 PNG 序列化）、`/api/crop`（Pillow 透明裁剪）与 `/api/inpaint`（Simple-LaMa 单次擦除所有掩码并集），完成自动抠图与背景擦除。
- [x] **3D 物体生成（TripoSR 优化）**：已部署 `/api/image-to-3d`（TripoSR + 背景去除/前景缩放），输出 GLB 网格；E2E 世界已验证生成真实模型。
- [x] **SFX 发生微服务**：已部署 `/api/generate-sfx` 碰撞/Foley 音效接口（当前采用 **AudioLDM-S**，非 Stable Audio Open；模型可后续替换）。
- [x] **测试与联调**：`backend/test_client.py` / `test_pipeline.js` 已就绪；2026-06-13 完成冒烟验证——后端健康检查 + CUDA + SAM2 实时分割（home 图检出 13 物体），前端全部路由 200、生成世界渲染、GLB 资产正常服务。

- [x] **SAM 3 概念分割（可切换）**：在保留 SAM 2 路径基础上，新增 SAM 3 概念/文本提示分割后端，输出**语义标签**并贯通到物体命名与 SFX 提示词；通过 `IMAGEWORLD_SEGMENTER=sam3` 开关启用，未安装时自动降级 SAM 2（详见 PROJECT_REPORT §3.6）。

- [x] **真实单图背景重建**：clean plate 已接入 Apple SHARP `/api/image-to-splat`，为每个世界生成独立 `.ply` 高斯溅射；仅在 SHARP 不可用时降级到 `home-room` 静态背景。

#### ⚠️ 2026-08-01 方向调整：放弃前景物体实例，管线收敛为纯场景重建

上述 SAM / TripoSR / AudioLDM / LaMa 前景链路**已从生成管线中移除**（后端端点保留，延迟加载不占显存）。理由：TripoSR 从单图猜测的几何与原物差距过大，且 SAM 自动分割选中的「最大 5 个区域」多为墙面、地板与门窗，而非可交互道具。

更重要的是，擦除前景对**场景本身**是净损失——它把照片里真实的家具几何抹掉，再用猜测填回。现在源图原样送入 SHARP，家具作为真实几何被重建进世界。

产品定位随之明确：**核心是一个可漫游的三维情景，而非情景里可以踢动的家具**。

效果：端到端耗时 数分钟 → **43 秒**，参与推理的模型仅剩 SHARP。详见 PROJECT_REPORT §3.7。

- [x] **本地开发工具部署门禁**：`/api/open-world-folder` 与 `/api/open-claude-terminal` 会在服务端机器上启动进程，生产环境默认返回 404（需同时设 `IMAGEWORLD_LOCAL_TOOLS=true` 开路由与 `NEXT_PUBLIC_IMAGEWORLD_LOCAL_TOOLS=true` 显示按钮）。因 BYOK 使项目变得可部署，该风险由理论转为现实。详见 PROJECT_REPORT §3.13。

### 阶段三：全栈 SaaS 系统开发 (Weeks 5-6)
- [ ] **用户与权限**：集成 Clerk 登录，开发管理控制台。
- [ ] **点数与计费**：设计点数消费表，集成 Stripe/Paypal，支持包月订阅和充值。
- [ ] **存储与分发**：配置 Cloudflare R2，实现 AI 生成文件自动上传并返回 CDN 加速链接。
- [ ] **推理队列系统**：实现后端收到生成请求 -> 插入 Redis 队列 -> AI 推理 worker 消费 -> 任务状态更新并写入 R2 和数据库 -> 实时 Webhook/SSE 通知前端。

### 阶段四：管线打通与精细化打磨 (Weeks 7-8)
- [x] **前后端全流程联调**：用户上传图片 -> SHARP 重建场景 Splat -> 提取碰撞体与地面标定 -> 加载进 3D Viewer -> 自由漫游和编辑。
- [ ] **加载与性能优化**：把 `.ply` 转为 **SPZ**（Spark 原生支持，体积约降一个数量级），首屏加载实现渐进式显示。当前单个世界 105 MB，其中 full_res PLY 占 64 MB。
- [ ] **碰撞与漫游手感**：改善 collider 质量（补洞、去噪），优化行走碰撞响应与相机手感。
  - [~] **可推动道具（`/api/extract-prop`）**：2026-08-12。把房间里的一件东西切成 dynamic 刚体，几何取自世界自己的 gaussians（与 2026-08-01 砍掉的 TripoSR 猜几何本质不同）。**原设想"连通分量=一件家具"被实测否掉**：三个世界里最大分量占 90–98%，站地上的东西在体素分辨率下与地板焊死，剩下的小分量多是浮空碎片。改为两条选区路径——SAM 2 点提示（精确但会选错）与手动 box（兜底），两条共用同一套下游。最难的是**深度门控**：mask 覆盖整条视线，不处理会把椅子背后的墙一起切出来；且不能按峰高选（墙的 gaussian 数常高一个量级，实测 500 vs 9000 时直接选中了墙），必须切在第一段空隙，且空隙宽度用米而非 bin 数。后端 + 渲染 + **web 端点选交互**均已完成并端到端实测（23 项后端测试、45 项前端测试）。浏览器里点顶部 **Free an object** 再点物体即可。坐标交接是最险的一环——collider 空间与 splat 空间不一致时提取仍会"成功"但切错位置，已用 4 组变换钉住不变量。详见 `backend/README.md` 与 PROJECT_REPORT §3.15。
  - [x] **表面分类与分材质碰撞**：2026-08-12。碰撞体不再是一整块——按"行走者会遇到什么"拆成 `floor` / `elevated` / `ramp` / `wall` / `ceiling` 五个 GLB 节点，前端各建独立 `RigidBody` 并给不同摩擦（地板 1.0 抓地，墙面 0.02 让人贴墙滑过而非被拖住）。两个不显然的点：**分类阈值不能与地板检测共用**（共用 `0.85` 时 36% 面积落进 `ramp`，拆开后降到 8.6–10.5%，三世界一致）；**`wall` 的 combine rule 比系数更重要**（Rapier 默认取平均，0.02 的墙实际解算成 0.26，必须用 `Min`）。已试过并**无效**：平滑法线再分类，`ramp` 反而从 35.9% 涨到 36.5%，不要重试。旧世界需重跑 collider 才生效，否则自动退回原路径。详见 PROJECT_REPORT §3.14。
  - [x] **出生点落在房间内**：此前硬编码 `z=-0.5`，实测位于房间几何之外 2.1 m（拍照者本就站在被拍空间外，故原点天然在几何外）。现由 collider 包围盒推算并写入 `scene.json` 的 `spawnPoint`，旧世界回退兼容。详见 PROJECT_REPORT §3.8。
  - [x] **可行走区域与 minimap**：2026-08-13。`build_collider.py` 顺带产出 walkable 掩码（能站 + 站得直 + **走得到**），前端 minimap（**左下角**——右下角被 384×384 的源图缩略图占着，最初放右下时被完全遮住）。第三道过滤是重点——单视角重建留下大量够得着走不到的碎片，画成可走区域等于在最该警告"前面是虚空"的地方给张骗人的地图。实测不可达比例：sunlit-office 9.6%、bounded 5.7%、**multiview-meetingroom 66.2%**（覆盖更广也更碎），这是"走进虚空"第一次被量化。两个坑：不能用 `triangles_center` 网格化（平坦区域=大三角形，地板几乎全漏，实测得出 0 m²）、可达面积不能大于可站面积（closing 填洞外扩，实测报出 −13.8% 这种自相矛盾数字）。详见 PROJECT_REPORT §3.17。
  - [x] **视觉验证打通**：2026-08-13。in-app 面板不渲染、本机 Chrome 访问不了 localhost，改用 Playwright 自带 Chromium（软件 WebGL2）终于看到了实际画面。抓到两个真 bug：**minimap 被 384×384 的源图缩略图完全遮住**（注释写着"放左边"实际用了 `right-3`）、**玩家标记 `top:120%` 飞出视口**（玩家在可行走区外是单视角世界的开场常态，未 clamp）。另有一个看似 bug 实非 bug：屏幕正中心那条射线严格水平，与地板永不相交且该高度正前方无几何，实测命中 0，而上下都命中 1–4 个——点空是常态，已补明确反馈。详见 PROJECT_REPORT §3.18。
  - [~] **场景边界**：已实现后又回滚（commit `98737e5` → `d61bce3`）。实测从出生点扫描 24 个方向，**8 个完全没有身体高度的几何**，且全部集中在相机背后 135°–210°——单视角拍不到自己背后，碰撞体因此不闭合（`is_watertight=False`）。人工围栏能解决，但世界模型生成 360° 场景后碰撞体会自然闭合，围栏即成冗余，故不为过渡期保留该机制。
- [x] **验证世界模型可行性（Marble 实测通过）**：2026-08-02 用 Marble API 生成一次，**封闭性 0/24 敞开**（SHARP 为 8/24）、地面面积 211 m²（SHARP 68 m²）、输出格式与本项目**原生兼容、前端零改动**。详见 PROJECT_REPORT §3.10。
- [x] **接入世界模型（BYOK）**：生成弹窗新增可选的 Marble API Key 输入框，附 [获取入口](https://platform.worldlabs.ai/api-keys)。**填了走 Marble、没填走本地 SHARP**，两条管线并存。Key 存于浏览器 `localStorage`，随请求传递，服务端不落盘、不入日志、仅置于 `WLT-Api-Key` 请求头。商业上等于**用户自带算力**，产品不承担 API 成本。
- [x] **多视图 SHARP（本地、免费、不换模型）**：2026-08-09 实现并实测。对走动视频的若干关键帧各跑一次 SHARP，用 COLMAP（`pycolmap`，纯 pip 装，无需 CUDA Toolkit / MSVC / COLMAP 二进制）求出的位姿把它们落到同一坐标系再体素融合。挂载点是 `unproject_gaussians` 本来就接受的相机外参——单图路径传的是 `torch.eye(4)`。
  - 实测（Tanks and Temples `meetingroom`，4 关键帧默认值）：可行走面积 42.4 → **193.8 m²**，有几何的方向 6/36 → **36/36**；提到 8 帧可达 272 m² 但画面明显更糊
  - 位姿精度对照数据集 ground truth：位置残差中位为轨迹尺寸的 0.337%，朝向误差中位 0.413°
  - **倾斜校正**是这里最关键的一步，也是单图路径原理上做不到的：世界系锚在手持相机上，实测继承了 **5.08°** 倾斜，15 m 跨度上造成 1.3 m 高差，把 21.7% 的点推到地板以下。用相机轨迹平面求"上"方向后转平，地板以下的点降到 **0.4%**，collider 房间高度从虚高的 5.35 m 回到 4.66 m。以轨迹为主而非地板：地板 RANSAC 在 11–18% 内点率下不稳定，同场景估计随关键帧数在 2.5°–5.4° 间漂移，而轨迹只依赖相机位姿，4/6/8 帧给出完全一致的 5.08°
  - **画质的天花板已经探明**：SHARP 各帧对同一表面的深度中位差 **28.6 cm**（偏置仅 +0.8 cm，是随机分散）。这就是"云雾感"的根因。opacity 阈值、尺度/各向异性过滤、孤立点剔除、多帧支持度、加粗融合体素——全部实测判别力 ≈ 1.0x（各向异性甚至 0.92x，比随机还差），细节见 `backend/README.md`。**不要重试这些**
  - 自由空间雕刻剔掉 18.6%，但视觉改善有限——只能剔观测表面前方的点，剔不掉被遮挡的错误几何和贴着表面的拉伸条纹
  - 唯一对症但未验证的方向：用 SHARP 的 `depth` 参数做两轮迭代对齐（第一轮的合并结果投影成共识深度图喂回第二轮）
  - 代价：full_res PLY 47 → 57 MB（4 帧）；CPU SfM 约 20 分钟（371 帧）；拍摄必须慢走，抽帧过稀会断链（实测注册率可从 67% 掉到 12%）
  - **注意**：上面的 229 m² 与下面 Marble 的 211 m² 是**不同场景**上测的，不可直接比较绝对值；能说的只是多视图把可行走范围拉到了与世界模型同一量级，且没有 API 成本。它仍然只重建"某个相机看见过的表面"，遮挡后方依旧空缺——这一点与世界模型有本质差别
  - 用法与完整实测数据见 `backend/README.md` §多视图重建；回归测试 `backend/test_multiview.py`
- [ ] **世界模型的长期取舍**：SHARP 的两个根本缺陷——走进虚空、遮挡处条纹涂抹——已确认是同一根因。BYOK 已让用户可自行选择；多视图路径大幅缓解了"走进虚空"（但没有消除遮挡涂抹）。若日后要自己承担生成成本，仍需在下列之间决策：
  - **Marble API**：$1.20/次、约 8 分钟、零运维、格式原生兼容、质量已验证。缺点是依赖外部服务与持续成本
  - **Matrix-3D 自部署**：MIT 许可、本机 16 GB 可跑（5B low-VRAM 约 12 GB），但慢且质量未验证。**注意：云 GPU 跑它约 $1–2/次，并不比 Marble 便宜**——自部署只有在本地 GPU 上才有成本优势
  - **HunyuanWorld-1.0-lite** 需 18 GB 超出本机，且许可排除欧盟/英国/韩国、设 100 万 MAU 上限
  - **多视图 SHARP**：零边际成本、几何真实（是实测而非生成），但要求用户拍视频而不是一张照片，且不填补遮挡
- [ ] **SPZ 迁移**：Marble 实测印证 SPZ 单位 splat 体积约为 PLY 的 1/4（13.5 vs 53 bytes）。若改用 Marble，此项自动完成；若继续自部署，则需在管线尾部加转换。
- [ ] **移动端适配**：优化手机端陀螺仪和触控操作，提升分享体验。
  - [x] **上手提示与截图**：2026-08-13。此前第一次进世界**没有任何操作说明**（手机上摇杆要等你猜到拖左半屏之后才画出来）；现在按触屏/桌面分别提示一次，9 秒或首次输入后消失。另加截图按钮——注意 canvas 无 `preserveDrawingBuffer`，必须在同一同步块内 `gl.render()` 后立即 `toDataURL`，否则只得到空白图。动手前的现状核查还发现三项"缺口"其实已存在（摇杆可视化、加载进度、侧栏画质切换）。详见 PROJECT_REPORT §3.16。

#### 2026-07-31 交付加固

- [x] 生产构建移除在线 Google 字体依赖，恢复 Next.js TypeScript 构建校验，Lint 零告警。
- [x] 生成路由增加 10 MB / PNG-JPG-WEBP 服务端校验、可配置后端地址与健康检查。
- [x] 世界生成采用隐藏暂存目录，成功后原子发布，失败时清理残留。
- [x] 创建弹窗增加后端状态、内联错误、键盘关闭、焦点样式与手机端滚动；通过 portal 修复侧栏动画导致的移动端定位偏移。

### 阶段五：上线与商业化推广 (Week 9+)
#### 2026-08-14 安全与单位加固

- [x] **API 面的来源校验**：`/api/*` 此前**零来源校验**——`multipart` / `text/plain` 是 CORS 简单请求，第三方页面能零交互清空 `scene.json`、占满 GPU，`<img src>` 就能触发起终端的路由。前端写请求统一带 `x-imageworld-app`，六条路由校验；**更要紧的是后端**：视频重建流程里浏览器直连 `localhost:8000`，Next 层守卫对真正占 GPU 的那一端毫无作用，已补 FastAPI 的 Origin 中间件（无 `Origin` 的 curl / 服务端调用照常放行）。另：uvicorn 默认绑 `127.0.0.1`、`/docs` 默认关闭、上传按请求计额度、服务端门禁改读不带 `NEXT_PUBLIC_` 前缀的变量（前缀版本会在构建期被烤进服务端 bundle，运行时关不掉）。详见 PROJECT_REPORT §3.19。
- [x] **删除零引用的 Marble key**：`.env.local` 里的 `WORLDLABS_API_KEY` 全仓库无任何代码读取（BYOK 走 localStorage + multipart），一个实时计费凭据只靠一行 gitignore 挡着。已删除，需去 platform.worldlabs.ai 吊销。
- [x] **单位归后端**：`CELL_SIZE=0.15` / `CHARACTER_HEIGHT=1.6` / `density=250 kg/m³` 此前作用在未缩放几何上。**8 个世界里 5 个 scale≠1**（demo-office 1.131、family-splatfacto 3.659），在 3.659 上净空门限实际是 5.9 m、面积差 13.4 倍、质量差 49 倍。**第一版方案被真实数据否掉**：整体缩放点云会连带改变 `--voxel-size` / `--max-distance` 的含义，实测在 24 m 场景上砍掉一半几何、`floor` 只剩 1%。收窄为只换算「以米上报」的量——walkable 网格与道具质量，碰撞体本身保持世界单位。walkable JSON 带上 `metric_scale_factor`，Minimap 按 `builtScale/liveScale` 定位，旧文件按 1 回退。详见 PROJECT_REPORT §3.20。
- [x] **坐标变换单一真源**：`WorldCollider` 与 `SplatRenderer` 改为 spread 同一个 `worldTransform()`；`propExtractor.test.ts` 此前是**第三份手写拷贝**，测的是它自己（把 SplatRenderer 的 offset 改成负号，9 个用例照样绿），现改为 import 真实模块并删掉恒真的往返用例。

- [x] **walkable 真正接进管线**：`build_walkable_map` 此前**唯一调用点**在 CLI 的 `main()` 里，浏览器生成的世界一个都没有 minimap（roadmap 已标 `[x]`，文案也已写着在做）。不能塞进碰撞体那一趟——视频世界的 `metricScaleFactor` 是**从碰撞体地板高度反推**的，网格要的 scale 那时还不存在。新增 `/api/walkable`，吃已建好的碰撞体 GLB（小一百倍且分类已在文件里），两条 Next 路由各在知道 scale 的时刻调用。老世界的单节点碰撞体返回 422、调用方降级——它们要 minimap 需重跑一次 collider。详见 PROJECT_REPORT §3.21。
- [x] **点选射线过滤**：`GroundPlane` 那块 200 m 隐形板与碰撞体共用 `DROP_TARGET_LAYER`，地板附近点击是掷硬币，且**任何朝下的落空点击都命中它**，让 §3.18 刚补的点空反馈几乎永不触发。改为按 `userData` 身份取"最近的房间命中"。
- [ ] **道具从碰撞体里减掉**：抠出的 dynamic 凸包与房间的 fixed trimesh 精确重叠，物理上推不动、视觉上多一块灰壳。**不能用互斥 collisionGroups 止血**（房间碰撞体含 floor/elevated，互斥后道具只剩 y≈0 平板兜着，会穿墙穿台面）。正确解是后端回传选区索引 + `build_collider(exclude=)` + 抠完重跑碰撞体 + splat 端 opacity 置零；数天工作量且需要自己的进度反馈，按审计结论**回退到分支做**。

- [ ] **SEO 优化与落地页**：制作令人惊艳的炫酷 3D 动效落地页，吸引早期流量。
- [ ] **Beta 测试**：放开免费试用限额，收集用户反馈。
- [ ] **商业化正式发布**。
