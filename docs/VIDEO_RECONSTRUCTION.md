# 视频重建接入

项目的视频入口使用本地 **COLMAP + Nerfstudio Splatfacto**。这条路线不是逐帧运行 SHARP
再拼点云，而是先从整段视频恢复统一相机位姿，再让所有已注册帧共同优化一个 3D Gaussian
场景，因此相邻视角的几何和颜色会一起收敛。

## 使用方式

1. 启动 `backend/server.py` 和 Next.js 开发服务。
2. 在“Create New World”中选择视频文件。
3. 选择质量档位，填写拍摄时的相机高度。用于导航训练时选择 `Navigation`。
4. 等待上传、SfM、训练、导出和项目打包完成；页面会自动进入新世界。

视频会直接上传到 FastAPI，避免由 Next.js 在内存中缓冲大文件。任务状态、日志和中间结果
位于 `.artifacts/reconstruction-jobs/<job-id>/`，刷新页面不会影响后端训练。后端异常退出后，
任务会变为 `interrupted`，可调用恢复接口继续使用已有 COLMAP 结果和最新 Nerfstudio 检查点。

## 接口

- `POST /api/reconstruction-jobs`：创建任务并上传视频。
- `GET /api/reconstruction-jobs`：列出任务。
- `GET /api/reconstruction-jobs/<job-id>`：读取进度与产物信息。
- `DELETE /api/reconstruction-jobs/<job-id>`：取消运行中的任务。
- `POST /api/reconstruction-jobs/<job-id>/resume`：恢复中断或失败的任务。
- `POST /api/video-reconstruction/finalize`：Next.js 将完成的任务打包为产品世界。

FastAPI 会用一个全局 GPU 锁串行化 Splatfacto 与已有 SHARP/分割等重任务，防止 16 GB 显存
上多个模型同时运行。打包阶段生成 full-res、500k、150k、100k 四级 PLY、缩略图、GLB
碰撞网格和清单。Nerfstudio 训练空间是 Z-up；打包时会把 splat、碰撞网格和训练相机统一
绕 X 轴旋转 -90° 到项目的 Y-up 坐标，并利用用户填写的相机高度恢复近似米制尺度。

## 导航训练质量模式

`Navigation` 使用 480 个候选帧、45,000 个训练步，并保留每个清晰采样窗口的帧，
同时把 COLMAP 的顺序匹配窗口扩大到 8 帧。训练开始前会检查三项硬指标：至少注册 24 帧、
注册率至少 75%、轨迹跨度与稀疏场景范围之比至少 0.05。该比值使用稀疏点云 5%–95%
分位包围盒的对角线归一化，不受 COLMAP 任意尺度影响，可以大于 1；它是移动程度检查，
并非已覆盖空间的百分比。任一指标不达标，任务会在 GPU 训练前失败并返回
重拍提示，避免把局部或断裂的相机轨迹发布成导航世界。

完成的任务会把 `registered_frames`、`extracted_frames`、`registration_rate`、
`trajectory_span`、`trajectory_length` 和 `trajectory_coverage` 写入任务状态，并复制到已发布世界的
`project.json` 和 `0-world.json`。导航档位还要求最终碰撞体至少有 1 m² 的可达地面，且可达面积
占可站立面积至少 40%，方便数据集筛选和导航训练追溯。

这些是基础筛选条件，**不等于通过 VLN 训练验收**。完整验收还检查机器人半径下的视点数、
连通分量、覆盖率、绕行程度及合格路线数，见 `docs/NAVIGATION_VALIDATION.md`。
`trajectory_span` 与 `trajectory_length` 记录 SfM 原始单位，不能直接解释为米。

## 拍摄建议

- 缓慢移动并保持连续视差，不要只站在原地旋转。
- 绕主体或空间走一圈并回到起点，有利于闭环。
- 避免大面积纯白墙、镜面、玻璃和严重运动模糊。
- 30–90 秒通常足够；导航模式建议覆盖多个相连空间，并让相机回到起点附近形成闭环。
- 注册率低于 75% 或注册帧少于 24 时，应优先改善素材或增加采样帧，而不是继续增加训练步数。

`Preview`、`Standard` 和 `Quality` 适合快速预览或没有导航覆盖要求的场景；`Quality` 已经
使用更密的帧采样，但不启用导航硬门槛。需要稳定用于导航训练时，优先使用 `Navigation`。
