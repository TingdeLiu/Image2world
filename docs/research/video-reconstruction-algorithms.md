# 视频到 3D 世界：重建算法调研与建议

> 调研日期：2026-08-09。仅引用论文、作者项目页、官方仓库与官方文档。目标机器为 Windows 11、RTX 5060 Ti 16 GB（本机 `nvidia-smi`：16,311 MiB，compute capability 12.0）。

## 结论先行

当前效果差的主因不是 SHARP 参数没调好，而是架构本身：每个关键帧分别做一次单目预测，再用外部位姿拼起来。每次预测得到的深度、表面厚度、颜色和遮挡解释都不同；刚性配准无法把这些互相矛盾的“局部世界”变成一个一致场景，所以关键帧越多，重影、雾层和漂浮物通常越严重。

**最值得先做的替换是：`视频关键帧 → COLMAP（必要时 VGGT 辅助）→ 一个场景、所有帧联合优化的 gsplat/Nerfstudio Splatfacto → PLY`。** 这不是简单换一个单帧模型，而是让所有观测共同优化同一组高斯。它直接针对当前的跨帧不一致，并且 Splatfacto 官方给出的显存占用约为 6 GB，`splatfacto-big` 约 12 GB，适合 16 GB 显卡。[Nerfstudio Splatfacto 官方文档](https://github.com/nerfstudio-project/nerfstudio/blob/main/docs/nerfology/methods/splat.md)

建议把 **AnySplat** 做成“快速预览/初始化”的并行实验，把 **VGGT 或 VGGT-Ω** 用于位姿和稠密几何初始化，不要直接把它们当作最终高保真世界。最终质量仍由多视图联合光度优化获得。2DGS/PGSR 更适合第二阶段的几何和碰撞网格实验。i3DGS 很有吸引力且原生验证了 Windows，但许可证只允许研究/评估，暂不适合作为可发布产品依赖。

## 为什么联合优化会明显优于逐帧 SHARP 合并

原始 3DGS 从 SfM 稀疏点初始化，在所有训练视角上交替优化同一组高斯的位置、协方差、不透明度和视角相关颜色，并进行增密/裁剪；它表示的是一个共享场景，而不是若干独立预测的并集。[原始 3DGS 官方实现](https://github.com/graphdeco-inria/gaussian-splatting)

这会带来三点结构性改善：

1. 同一面墙必须同时解释多个视角，因此深度冲突会通过重投影误差被共同纠正，而不是在合并后形成两层墙。
2. 遮挡、可见性和颜色通过可微渲染共同优化，减少简单体素择优无法消除的漂浮高斯。
3. 关键帧可以增加覆盖面，而不必为每一帧额外叠加一整套百万级单目高斯。

这类重建仍然只恢复视频中被充分观察的区域。它不会可靠生成从未拍到的房间、物体背面或遮挡后方；纹理弱、镜面、玻璃、运动物体和严重运动模糊仍会失败。AnySplat、VGGT、CUT3R 等使用学习先验的方法可能在欠约束区域给出“合理”几何，但那是先验推断，不是观测事实，不应作为精确碰撞或测量依据。

## 候选方法比较

| 路线 | 适合在本项目中做什么 | 16 GB / Windows 可行性 | 许可与产品风险 | 集成工作量 |
|---|---|---|---|---|
| **gsplat + Nerfstudio Splatfacto** | 首选最终重建器；消费现有 COLMAP 数据，联合优化单个 3DGS 并导出 PLY | 官方文档：默认约 6 GB，big 约 12 GB；gsplat 提供部分 PyTorch/CUDA 组合的 Windows wheel。Nerfstudio 官方仍建议有条件时用 Linux，Windows 可用但构建更脆弱 | gsplat、Nerfstudio 均 Apache-2.0，商业使用最清晰 | **低—中**：现有 SfM 输出已接近 COLMAP 数据结构，主要是训练封装、坐标/PLY 元数据适配和进度管理 |
| **原始 Inria 3DGS** | 质量基线、论文复现 | 官方称论文质量训练需 24 GB；可用降低分辨率、把图像放 CPU 等方式压显存。官方测试过 Windows 10 | 自定义许可证，仅研究/评估，商业使用需授权 | 中；Windows CUDA 扩展构建与显存都不如 gsplat 方案合适 |
| **VGGT** | COLMAP 失败时预测相机/深度/点图；可导出 COLMAP，并可选 BA；也可给 3DGS 稠密初始化 | 官方支持 BF16 推理，2026-05 修复后同显存可处理约 2–3 倍帧数；Windows 没有专门承诺，但主体是 PyTorch。16 GB 应采用分段/精选帧 | 代码与 `VGGT-1B-Commercial` 检查点可商业使用（禁止军事用途、需申请）；原始 checkpoint 仍非商业 | 中：增加回退前端、置信度过滤、与 COLMAP/BA 的选择逻辑 |
| **VGGT-Ω** | 更长序列、更稳定的相机与深度研究前端 | 官方 A100 实测：50 帧 9.66 GB、100 帧 13.37 GB、200 帧 20.82 GB；16 GB 建议 50–80 帧或 `max_size` 缩放，需给 Windows/训练器留余量 | FAIR Noncommercial Research License；输出也不得用于商业用途 | 中；API 简单，但 checkpoint 需申请，不能作为商业发布依赖 |
| **AnySplat** | 快速直接从未标定图像/视频得到一个统一 GS；适合秒级预览，或作为后续优化初始化 | 官方用 PyTorch 2.2/CUDA 12.1、448² 输入，未给显存表或 Windows 保证；仓库称其他 PyTorch/CUDA 版本应可用。RTX 5060 Ti 需用支持 Blackwell 的新 PyTorch/CUDA 组合实测 | 仓库和权重标为 MIT；仍应审计其依赖和随附模型条款 | 中：官方已有视频上传、PLY/渲染路径和 `post_opt`；输出字段需适配当前 viewer/collider |
| **CUT3R** | 持久状态的连续视频几何/位姿前端，弱纹理和非标准相机轨迹的研究备选 | CUDA 12.1，并需编译自定义 RoPE CUDA kernel；官方未承诺 Windows或 16 GB | CC BY-NC-SA 4.0，不能作为商业产品核心 | 高：环境老练度、输出转换、尺度与后端优化均需自行处理 |
| **MASt3R-SLAM** | 当前较强的 RGB 视频稠密 SLAM/位姿前端；可替代 COLMAP 前端做研究 A/B | 官方实验用 RTX 4090；主要测试 Ubuntu，提供专门的 WSL `windows` 分支；16 GB 需要降低分辨率/关键帧率实测 | CC BY-NC-SA 4.0；checkpoint 还需单独核查 | 高：大型 CUDA/SLAM 栈，适合独立服务或 WSL，不适合先塞进现有后端环境 |
| **2D Gaussian Splatting (2DGS)** | 强化表面、法线、深度与网格；适合碰撞体/可导航几何质量实验 | CUDA 扩展；官方提供 Windows SIBR viewer，但没有明确承诺 Windows 训练器或 16 GB。一般应先在 WSL/Linux验证 | 继承 Inria 研究/非商业许可证 | 高：表示从 3D 椭球变为有向 2D 圆盘，当前 PLY/viewer/collider 都要适配 |
| **PGSR** | 平面室内场景的高质量表面与网格，几何导向很强 | 官方未给 Windows/显存保证；基于定制 3DGS CUDA 栈 | 仅教育、研究、非营利；修改件也要求开源，商业需联系作者 | 高；更适合作为离线几何分支，不应阻塞第一阶段画质改善 |
| **i3DGS (SIGGRAPH 2026)** | 大规模/无序图像、快速增量反馈、回环和全局一致性；是很有价值的研究标杆 | 官方明确测试 Windows 11、PyTorch 2.7.1、CUDA 12.8、MSVC v142；与 RTX 5060 Ti 的 Blackwell 环境匹配度高，但未发布显存表，16 GB 仍需实测/降分辨率 | Inria Immediate3DGS，仅研究/评估；商业需联系 OnTheFly | 中—高；工程完整但许可阻止直接产品化 |

来源：[gsplat 官方仓库与显存对比](https://github.com/nerfstudio-project/gsplat)、[VGGT 官方仓库](https://github.com/facebookresearch/vggt)、[VGGT-Ω 官方仓库和显存表](https://github.com/facebookresearch/vggt-omega)、[AnySplat 官方仓库](https://github.com/InternRobotics/AnySplat)、[CUT3R 官方仓库](https://github.com/CUT3R/CUT3R)、[MASt3R-SLAM 官方仓库](https://github.com/rmurai0610/MASt3R-SLAM)、[2DGS 官方仓库](https://github.com/hbb1/2d-gaussian-splatting)、[PGSR 官方仓库](https://github.com/zju3dv/PGSR)、[i3DGS 官方仓库](https://github.com/graphdeco-inria/i3dgs)。

### Gaussian SLAM 是否应该直接取代离线流程？

不建议把 Gaussian SLAM 作为第一步替代。SLAM 的首要目标是在线跟踪和增量建图，最终新视角质量通常仍受每帧有限优化预算影响；项目接收的是已经拍完的视频，可以使用全片信息、回环和更多迭代，离线联合优化更合适。

可作为对照的官方实现包括：

- **MASt3R-SLAM**：RGB-only，直接接 MP4，使用 3D 重建先验；研究价值最高，但非商业且官方实验基于 RTX 4090。[官方仓库](https://github.com/rmurai0610/MASt3R-SLAM)
- **Splat-SLAM**：RGB-only、全局位姿图和可变形 3DGS，Apache-2.0；但官方仓库已于 2026-03-10 归档，依赖旧 PyTorch/CUDA 和多个定制 CUDA 模块，无 Windows 指南，不适合作为新产品基线。[官方归档仓库](https://github.com/google-research/Splat-SLAM)
- **MonoGS**：支持单目/RGB-D/实验性双目，官方主要测试 Ubuntu 与 RTX 4090，且提醒避免激烈运动；适合论文对照，不如“稳定位姿 + 离线 Splatfacto”符合当前任务。[官方仓库](https://github.com/muskie82/MonoGS)
- **SplaTAM**：BSD-3-Clause 且有 Windows 社区安装说明，但核心是 RGB-D；若以后允许 iPhone LiDAR/深度输入，它会比纯 RGB 视频更有意义。[官方仓库](https://github.com/spla-tam/SplaTAM)
- **Gaussian-SLAM**：MIT，官方只测试 Ubuntu/CentOS 的 RTX 3090/A6000，偏 RGB-D 数据集；可作为带深度捕获的候选，不是当前普通 MP4 的直接替代。[官方仓库](https://github.com/VladimirYugay/Gaussian-SLAM)

## 推荐的分阶段架构

### 阶段 1：先替换重建核心（最高收益、最低风险）

```text
MP4
  → 去模糊/曝光筛选 + 动态区域 mask
  → COLMAP/pycolmap 相机、稀疏点、去畸变关键帧
  → Nerfstudio Splatfacto（所有关键帧共同优化一个场景）
  → 导出标准 3DGS PLY
  → 现有 LOD / viewer / collider
```

实施建议：

- 先用 40–100 张空间分散、重叠充足的帧，而不是当前仅 4–8 张 SHARP 帧；帧数由相邻基线、视差、模糊和覆盖率决定，不按固定时间间隔。
- 默认跑 `splatfacto`；确认 16 GB 余量后再 A/B `splatfacto-big`。Windows 原生失败时优先把训练器隔离到 WSL2/Linux 服务，前后端无需迁移。
- 保留当前 PLY 兼容层，但不要伪造 SHARP 独有相机元数据；场景相机、尺度和坐标系写入单独 manifest，避免下游把“第一张图的相机”误当整个世界的相机。
- 给人、屏幕、窗外、反光和明显运动物体生成 mask；这往往比继续调体素阈值更能减少 floaters。

### 阶段 2：增强位姿与初始化

采用双前端而不是只押一个模型：

1. **COLMAP 主路径**：纹理充分、静态、重叠好的视频仍优先使用几何 SfM；结果可解释、成熟且商业许可清晰。
2. **VGGT 回退/初始化**：当 COLMAP 注册率低、焦距漂移或长走廊回环失败时，运行 VGGT，按置信度过滤深度/点图，导出 COLMAP 格式，再做 BA。VGGT 官方已经提供 `demo_colmap.py --use_ba`，输出可直接喂给 gsplat。[VGGT COLMAP 导出说明](https://github.com/facebookresearch/vggt#exporting-to-colmap-format)
3. **研究开关 VGGT-Ω**：用 50–80 帧验证长序列位姿，暂不进产品路径，因为官方许可证明确限制非商业研究。

### 阶段 3：快速预览与最终质量分层

- 上传后先用 **AnySplat** 生成快速可交互预览；它从未标定多视图一次性预测高斯、深度和相机，并提供官方 `post_opt`。
- 后台继续跑 COLMAP/VGGT + Splatfacto，完成后原子替换 preview PLY。
- 做同一测试视频的定量 A/B：训练视角 PSNR/SSIM、留出视角 LPIPS、重投影深度一致性、相机轨迹抖动、浮点率、PLY 大小、训练时间和峰值显存。不要只看训练视角截图。

### 阶段 4：几何和碰撞单独优化

渲染最漂亮的 3DGS 不等于碰撞最准确。先保持 3DGS 为视觉层，再独立生成几何层：

- 近期：从联合优化后的深度/高斯多视图融合 TSDF，再做地面/墙体平面约束和 mesh 清理。
- 研究：A/B 2DGS 或 PGSR 的 mesh extraction；室内平面多时 PGSR 值得试，但两者许可证都不适合直接商业发布。
- 如果能改变采集端，优先加入 ARKit/ARCore/RealSense 的深度、IMU、内参和重力方向；这对碰撞、水平面和米制尺度的提升通常大于再换一个 RGB-only 网络。

## 必须保留的约束：尺度与“看不见的区域”

纯单目视频的几何存在整体相似变换歧义。联合 3DGS、COLMAP 或普通 RGB SLAM不会凭空提供可靠绝对米制尺度。替换 SHARP 后必须新增至少一个尺度锚：已知相机高度/物体尺寸、手机 AR 位姿与深度、IMU/重力，或让用户标定两点距离。学习模型给出的近似米制深度可以做初值，不能替代可验证的尺度约束。

同样，任何观测式重建都不能恢复从未拍到的房间。若产品需要“完整世界”，应明确拆成两层：

- **reconstructed / observed**：由视频证据支持，可用于碰撞和测量；
- **generated / inferred**：生成式补全，只用于视觉，并在 manifest 中标记低置信度，默认不生成碰撞。

## 推荐决策

1. **现在实施**：COLMAP → Splatfacto，彻底替换多帧 SHARP merge；保留 SHARP 仅作为单图模式。
2. **同时做两项小型 A/B**：VGGT→COLMAP/BA→Splatfacto，以及 AnySplat→官方 post-opt。两者都用同一批关键帧和留出视角评测。
3. **研究而不入产品**：VGGT-Ω、i3DGS、MASt3R-SLAM、2DGS、PGSR；先验证增益，再处理许可或寻找 Apache/MIT 重实现。
4. **不要先做**：继续增加 SHARP 关键帧、加重体素平均或仅更换自由空间雕刻阈值。这些只能压制症状，无法修复跨帧深度不一致。

## 主要官方来源

- [3D Gaussian Splatting：官方实现、Windows/硬件与许可证](https://github.com/graphdeco-inria/gaussian-splatting)
- [gsplat：Apache-2.0、Windows wheel、显存/速度对比](https://github.com/nerfstudio-project/gsplat)
- [Nerfstudio Splatfacto：数据初始化、显存档位与训练命令](https://github.com/nerfstudio-project/nerfstudio/blob/main/docs/nerfology/methods/splat.md)
- [Nerfstudio Windows 安装说明](https://github.com/nerfstudio-project/nerfstudio/blob/main/docs/quickstart/installation.md)
- [VGGT：相机/深度/点图、COLMAP 导出、商业 checkpoint 说明](https://github.com/facebookresearch/vggt)
- [VGGT-Ω：长序列显存表与许可证](https://github.com/facebookresearch/vggt-omega)
- [AnySplat：未标定多视图到统一高斯、视频 demo 与 post-opt](https://github.com/InternRobotics/AnySplat)
- [CUT3R：连续 3D 感知与 CC BY-NC-SA 许可](https://github.com/CUT3R/CUT3R)
- [MASt3R-SLAM：视频输入、WSL 分支、RTX 4090 实验与许可](https://github.com/rmurai0610/MASt3R-SLAM)
- [2DGS：表面导向 Gaussian 与 mesh extraction](https://github.com/hbb1/2d-gaussian-splatting)
- [PGSR：平面高斯表面重建与非商业许可](https://github.com/zju3dv/PGSR)
- [i3DGS：Windows 11/CUDA 12.8 支持、全局一致性与研究许可](https://github.com/graphdeco-inria/i3dgs)
- [Splat-SLAM：RGB-only 全局 Gaussian SLAM（已归档）](https://github.com/google-research/Splat-SLAM)
- [MonoGS](https://github.com/muskie82/MonoGS)、[SplaTAM](https://github.com/spla-tam/SplaTAM)、[Gaussian-SLAM](https://github.com/VladimirYugay/Gaussian-SLAM)
