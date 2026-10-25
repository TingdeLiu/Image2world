# 视频重建质量与相机记录

视频重建现在会在 Splatfacto 导出后增加“Checking held-out views”阶段。评测结果随世界保存，在左侧展开当前世界的 **Reconstruction quality** 可查看指标、原始观测 / 渲染对比图和相机文件。

## 测什么

使用训练配置实际划分出的评测图片，与模型从对应相机渲染的图片比较。沿用已安装 Nerfstudio 的 PSNR、SSIM 和 LPIPS 实现，分别记录每个视角、均值和总体标准差。新任务将训练、验证、测试视角分开；预览查看验证组，最终发布查看测试组，旧任务保留原划分。实际数量以报告为准，小数据集可能没有评测图片。见 [预览与续训说明](STAGED_RECONSTRUCTION.md)。

- PSNR、SSIM 越高越好；LPIPS 越低越好。没有人为设置一个“达到多少就是好世界”的门槛。
- 评测前检查训练 / 评测文件的真实路径，以及图片文件的 SHA-256。重复文件和改名后的相同图片不会被当成独立证据。
- 没有保留视角、图片重合、依赖或 GPU 评测失败时，报告显示 `unavailable`，不会伪装为质量通过。重建本身仍可完成。
- 最多保存三张对比图：左侧原始观测，右侧模型渲染。支持 Nerfstudio 的 uint8 图片缓存和浮点图片。

这是**外观在保留视角上的验证**。相机估计和 SfM 初始点云仍可能使用全部输入帧；这些指标不能证明几何正确、导航可用、背面真实，或整个房屋已经补全。导航仍使用现有碰撞体、可走区域、VLN 图与视点渲染检查。

## 保存什么

任务工作目录：

```text
.artifacts/reconstruction-jobs/<id>/work/
  cameras.json                 所有输入相机在高斯资产坐标中的位姿、内参与图像尺寸
  evaluation/report.json       评测协议、样本数、逐视角与总体指标、不可评测原因
  evaluation/comparison_*.jpg  最多三张观测 / 渲染对比图
```

发布后进入 `public/worlds/<slug>/output/world/`：

```text
0-world-quality.json
0-world-quality-comparison-0.jpg  # 最多三个，评测成功才复制
0-world-cameras.json
0-world.json                     # assets.reconstruction 保存摘要与尺度来源
```

浏览器下载地址由扫描器根据实际存在的本地文件生成；不会采用清单中任意填写的外部地址。旧世界没有这些记录时不显示质量面板。

## 相机、坐标与尺度

相机文件使用 OpenGL 相机约定（右、上、后），位姿为 camera-to-world。原始数据集相机经过训练保存的 dataparser 刚体变换，再缩放平移，进入导出的 splat 坐标；没有应用展示用的居中或单位立方体缩放。

相机记录来自 COLMAP 与 dataparser，早于训练期间可选的 camera optimizer 调整，不冒充优化后的逐帧相机。文件保存来源和变换，并在发布时附上 `splat_to_initial_viewer` 矩阵，组合初始的旋转、尺度与地面平移。编辑器以后的修改由 `scene.json` 表达。

COLMAP 的单位本来是任意单位。只有相机高度标定有效且缩放落在支持范围内，才标为 `camera-height-estimate`；缺少标定或标定被拒绝时标为 `arbitrary`。由输入高度估算的米制尺度仍然是估计值。缩放因子恰好等于 1 时，也保留它是否经过标定的区别。

## 执行与复核

从创建界面上传视频即可自动执行，无需安装 GAE 或下载新模型。既有 Splatfacto 环境负责渲染与 LPIPS；Windows 包装器沿用现有 CUDA 和本地产生检查点的 PyTorch 兼容配置。

命令行流程同样默认评测：

```powershell
python backend/tools/splatfacto_prototype.py <video-or-image-directory> --out <work-directory>
```

已有工作目录可通过 `--reuse-sfm --reuse-training` 重用相机与训练检查点，重新导出和评测。`--skip-evaluation` 可跳过评测，但结果会明确记录为未评测，不会保留旧分数。

独立评测器为 `backend/tools/evaluate_reconstruction.py --load-config <config.yml> --output <report.json>`，应在同一个 Nerfstudio 环境和 CUDA 配置下运行。标准差采用总体标准差，因此只有一个评测视角时也能输出有效 JSON。

## 本次实际验证

使用现存 `.artifacts/splatfacto-meetingroom-5k/` 检查点，没有重新训练模型：98 个训练视角、10 个保留视角；PSNR **19.46 dB**、SSIM **0.699**、LPIPS **0.535**。相机导出包含 108 帧。结果保存在 `.artifacts/gae-borrowing-validation/meetingroom/`。

这些数字用于证明评测流程确实跑通和记录当前基线，不代表重建质量已经改善，也不与不同数据或配置的分数直接比较。
