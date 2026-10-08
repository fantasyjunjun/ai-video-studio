---
id: spray-ritual
title: 喷香水镜头的唯一合法写法（铁律 6 / 7 / 27 合并）
tags: [prompt, spray, always]
priority: 1
---

# 喷香水镜头

香水广告的核心动作是"喷"，成片至少要有**一个真实可辨的"手指按压喷头 → 汽雾喷出"镜头**。它是"禁止状态改变"铁律的**边界情况**，必须同时满足条件。

## 前置条件（两种已验证写法，二选一）

- **A（最稳，默认）盖子不在画面内**：`the bottle is already uncapped in the first frame; the cap is not in the frame`
- **B（可露出瓶盖）盖子已躺在台面上**：`the black dome cap is already lying on the table beside the bottle at frame one` + 反向锁 `the cap stays lying on the table the whole time and never moves onto or off the bottle`

**两者都禁止出现"拿起/拔开/旋开盖子"的动作**（会退化成盖子凭空消失）。反向约束写：
`Do not show any uncapping, twisting or removing of the cap; the bottle is already open. No mist before the press.`

## 雾的落点：二选一

### 写法 A（默认，近距落肤）

喷头在距皮肤几厘米处按下，雾锥**只有几厘米长**、抵达皮肤即散：
`only a few centimetres long: it reaches the skin and disperses there`
落点写清"只留极淡均匀光泽"，反向锁死 `no raised droplets, no bead, no drip`（防皮肤挂水珠穿帮）。

### 写法 B（喷空中 + 转圈走入光折射汽雾，已合法化）

用于"活泼又性感"的喷空入雾。**单镜可行**，六条必须同时满足：

1. **顺序锁死**：`the mist never appears before the press`；
2. **先原地转圈、再朝汽雾走去**（脚可移动几步）：`she turns on the spot then walks a few steps forward into the vapor`，转约 120°–180°，肩/颊/发穿过悬于光束前的薄透汽雾；
3. **中景 50mm**（不是 85mm 近景），且 `[SHOT]` 显式写景别与背景、不被参考图框死；背景可用精细提示词稳定（如暗调大理石浴室 + 独立浴缸 + 背后竖直灯板）；
4. **汽雾必须处在一条光束里才可见** —— 垂直发光灯板在主体背后（practical light ~3200K）+ 硬逆光 rake 穿过雾。没有这条，水汽在暗背景里不可见，整镜塌掉；
5. **汽雾是薄透、接近透明、光下带金色反光、不遮脸**：`thin translucent vapor ... catches a faint golden sheen ... never obscures the face`。**严禁写成白雾/浓雾/烟雾**。喷发在约 0.5s 内结束，之后汽雾在空中缓缓落下；人物**微笑**；
6. **反向锁死第二个人及其任意部位**：`no second person, no second person's hands or any other body part`（大角度转身易生成第二个人或其手/脸/手臂等部位入镜，须强制画面里所有部位都属于她本人；2026-10-01 用户升级：禁的不只是"第二只手"，是第二个人及任何部位）。

## 已知未达成项（别指望提示词彻底解决）

- **"持续出雾/持续喷"写"停止"无效**：必须显式写终止条件（`one short burst … and then stops` + `never keeps flowing`），但**画面侧仍不一定停**。
- **模型对结果性元素有固有前摇**，且**前摇不是常数**（同一工作流同一 4s 请求，随提示词变化 0.72s–1.10s）。每次换提示词都必须重新实测起点，不能复用旧数。
- **不要写自相矛盾的约束**（如 `her finger does not lift off again` 与"喷出后停止"物理互斥，会互相抵消）。
