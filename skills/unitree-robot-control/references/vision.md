# 视觉获取

## 先记住一句话

**不是所有宇树机器人都能给你图像。** 实测的 R1 **Air 完全没有可用相机**：
WebRTC 20 秒内没有任何视频 track、RTSP 服务只有推流端（`ANNOUNCE/RECORD`）且所有
挂载点 404、固件拍照 API 也没有可用的 handler。

所以视觉部分的正确用法是：**先探测，再抓图**。

```
robot_vision_probe      # 哪条路能用？为什么不能用？
robot_snapshot          # 抓一张
robot_snapshot_history  # 之前抓的在哪
```

## 四条来源，按 `auto` 尝试顺序

| 来源 | 名字 | 状态 | 说明 |
|---|---|---|---|
| 机器人视频轨 | `webrtc` | ✅ 已有相机机型可用 | 走已有的 WebRTC 链路，不额外开端口，只在抓帧时打开视频通道 |
| 固件拍照 API | `photo_api` | ⚠️ **未验证** | `rt/api/videohub/request`。上游只有 topic 常量、没有任何调用示例，api_id 未知 |
| 机器人 RTSP | `rtsp` | ⚠️ **未验证** | GStreamer RTSP，通常在 `:8551`。具体机型/固件是否提供直播挂载点未知 |
| 本地文件 / 摄像头 | `file` / `device_camera` | ✅ | **离线开发用**，不需要机器人 |

`auto` 会按上表顺序依次尝试，第一个成功的就返回；全失败时把**每条路的具体失败原因**
拼在 `detail` 里。

## 用本地源离线开发（强烈推荐）

没有机器人、或机器人没有相机时，整条视觉链路（编码、缩放、归档、MCP 投递）
都可以用本地源跑通：

```bash
# 用一张图片（或一个视频、或一个目录里最新的那张）
export UNITREE_AGENT_VIDEO_SOURCE=file
export UNITREE_AGENT_SNAPSHOT_FILE=/path/to/frame.jpg     # 或 /path/to/clip.mp4 或一个目录

# 或者用本机摄像头（macOS: avfoundation 索引）
export UNITREE_AGENT_VIDEO_SOURCE=device_camera
export UNITREE_AGENT_CAMERA=0
```

然后 `robot_snapshot` 就能返回图像了——即使 `--backend mock` 也一样。

## 配置项

| 变量 | 默认 | 含义 |
|---|---|---|
| `UNITREE_AGENT_VIDEO_SOURCE` | `auto` | `auto` / `webrtc` / `photo_api` / `rtsp` / `device_camera` / `file` |
| `UNITREE_AGENT_SNAPSHOT_MAX_WIDTH` | `1280` | 抓帧最大宽度，超过则等比缩小（1080p JPEG 约 200 KB，缩小后省很多） |
| `UNITREE_AGENT_SNAPSHOT_DIR` | `<state_dir>/snapshots` | 抓帧归档目录 |
| `UNITREE_AGENT_SNAPSHOT_FILE` | — | 本地图片/视频/目录（离线用） |
| `UNITREE_AGENT_CAMERA` | — | 本地摄像头索引（离线用） |
| `UNITREE_AGENT_RTSP_URL` | — | 显式指定 RTSP 地址；不设则跳过 RTSP |
| `UNITREE_AGENT_PHOTO_API_IDS` | `1001,1002,1003,1004,1005,1006` | 拍照 API 要试哪些 api_id |

> **RTSP 默认关闭**是有意的：这台 R1 的 8551 虽然开着，但没有任何可用挂载点，
> 若默认去试，每次 `auto` 抓帧都会白白多等几秒超时。

## 归档

每次抓帧都会写入 `<state_dir>/snapshots/`：

- 文件名：`YYYYmmdd_HHMMSS_<source>.jpeg`
- 索引：同目录 `index.jsonl`，一行一条（时间、路径、来源、尺寸、字节数）
- 上限：最多 500 条，超出后自动删除最旧的（含文件）

`robot_snapshot_history` 读的就是这个索引，并会标出文件是否还存在。
**"下指令时机器人看到的是什么"** 这类问题靠它回答。

## 图像是怎么编码的

只用 PyAV（`aiortc` 自带），不依赖 OpenCV / Pillow：

```
av.VideoFrame → CodecContext(mjpeg, yuvj420p) → 裸 JPEG 字节
```

不用 muxer 是有原因的（都是实测踩出来的坑）：

- `image2` 是**文件型** muxer：写成 `BytesIO` 时 `encode()` 会产出 packet，
  但 `mux()` **静默什么也不写**——拿到 0 字节且不报错。
- `mpjpeg` 确实会写，但会在 JPEG 外面包 multipart 边界，产出的字节不再以
  JPEG magic 开头。

这两个坑在只有离线测试的情况下才暴露出来（唯一能上手的机器没有相机，
这条路径从来没有在真机上跑过）。

## 失败时怎么排查

`robot_snapshot` 失败会返回：

```json
{
  "ok": false,
  "error": "no_frame",
  "message": "no source produced a frame",
  "detail": "webrtc: ... | photo_api: ... | rtsp: ...",
  "hint": "Run robot_vision_probe to see every source and why it failed."
}
```

按 `robot_vision_probe` 的输出处理：

| 输出 | 含义 | 处理 |
|---|---|---|
| `webrtc: no video track arrived` | 这台机型没有相机，或固件不通过 WebRTC 暴露 | 换机型验证，或用本地源离线开发 |
| `photo_api: ... tried 1001:code=127` | 该 api_id 在这台固件没有 handler | 用 `UNITREE_AGENT_PHOTO_API_IDS` 换一组 id 试 |
| `rtsp: no RTSP url configured` | 没配地址 | 配 `UNITREE_AGENT_RTSP_URL`，或接受没有 RTSP |
| `rtsp: ... 404 Not Found` | 服务在，但没有该挂载点 | 换挂载点，或该机型的 RTSP 只用于推流 |
| `no file found at ...` | 路径不存在 | 检查 `UNITREE_AGENT_SNAPSHOT_FILE` |
