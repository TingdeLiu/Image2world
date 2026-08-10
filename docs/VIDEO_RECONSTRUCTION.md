# 视频重建接入

项目的视频入口使用本地 **COLMAP + Nerfstudio Splatfacto**。这条路线不是逐帧运行 SHARP
再拼点云，而是先从整段视频恢复统一相机位姿，再让所有已注册帧共同优化一个 3D Gaussian
场景，因此相邻视角的几何和颜色会一起收敛。

## 使用方式

1. 启动 `backend/server.py` 和 Next.js 开发服务。
2. 在“Create New World”中选择视频文件。
3. 选择 `Preview`、`Standard` 或 `Quality`，填写拍摄时的相机高度。
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

## 拍摄建议

- 缓慢移动并保持连续视差，不要只站在原地旋转。
- 绕主体或空间走一圈并回到起点，有利于闭环。
- 避免大面积纯白墙、镜面、玻璃和严重运动模糊。
- 30–90 秒通常足够；注册率低于 60% 时应优先改善素材或增加采样帧。

质量档位主要改变采样帧数、训练步数和渲染缩放。先用 `Preview` 验证素材能否稳定注册，
再对同类高质量素材使用 `Quality`，通常比盲目提高训练步数更节省时间。
