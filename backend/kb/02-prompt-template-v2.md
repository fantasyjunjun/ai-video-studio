---
id: prompt-template-v2
title: v2 提示词模板（运动优先重排）
tags: [always, prompt, template]
priority: 1
---

# v2 提示词模板

**为什么这样排序**：视频模型对**开头 token 权重更高**，而"运动"是最难的部分。先给运动保证它在预算内；身份描述后置，因为它有参考图兜底。

```text
[SHOT]    <一句话：这一镜的主动作是什么，≤15 词。若需自由设定景别/背景，在此显式写明>
[HERO]    <画面里唯一清晰焦点；其余元素显式降级>
[MOTION]  <动作如何发生 + 时间位置：at the start / over the first second / unchanged through the clip>
[CAMERA]  <量化运镜（带位移量）+ 明确不做什么（no cut / no zoom / no rotation）>
[LIGHT]   <光位 + 角度 + 光比 + 色温；用可执行参数>
[PHYSICS] <结果性元素的起点 / 方向 / 亮度关系（无结果性元素则省略此层）>
[TEXTURE] <本镜要 highlight 的 2–3 个材质反应，不要罗列全部>
[REF]     <≤25 词的锚点：与参考图保持一致>
[NEG]     <祈使句反向约束 3–5 条，按本镜**具体风险**裁剪，禁止抄模板>
```

## 硬性量化要求

| 项 | 要求 | 反例 / 正例 |
|---|---|---|
| 运镜 | **量化 + 锁死** | ❌ `extremely slow dolly in` → ✅ `dollies forward by about 10% of the frame width, constant speed. No cut, no zoom, no rotation, no shake.` |
| 锚点 | `[REF]` ≤25 词 | ❌ 100+ 词逐条描写五官 → ✅ `The same woman as reference image 1 and 2 — same face, same champagne satin dress.` |
| 节拍 | ≤3s→1 个 / 4–6s→2 个 | 写不下就**拆镜**（多一镜几毛钱，写废一镜要重跑） |
| 风格 | **可被摄影指导照着布光的参数** | ❌ `cinematic warm color grade` → ✅ `a single hard key at 45° camera-left, 4:1 ratio, a black flag on the right cutting the fill` |
| 物理锚点 | 结果性元素必须绑 **起点 + 方向 + 亮度关系** | ✅ `a thin cone of droplets that originates exactly at the nozzle tip, opens outward in the same direction the nozzle points, brightest where it leaves the nozzle` |

## 物理锚点对照表

| 元素 | 必须绑 |
|---|---|
| 雾 / 喷雾 | 喷头（起点）+ 喷头朝向（方向）+ 光束（可见性） |
| 水珠 | 杯壁/瓶壁（附着面）+ 重力方向 |
| 烟 | 香尖/火源 + 上升方向 |
| 飞溅 | 落点 + 初始动能方向 |
| 飘落物 | 来源（枝/手）+ 风向 |

## 追求"模型出字"是伪需求

标签小字只在瓶身占画面 60% 以上的大特写里能出；拉远后会漂。提示词**只保证标签几何平整、无畸形反光**，所有要清晰的文字一律**后期叠加**。

## 失败降级阶梯（按顺序退，别乱改）

1. 砍节拍，只留 1 个动作 → 2. 高危部位出画 → 3. 去掉人物改纯产品镜 → 4. 换更近景别 → 5. 放弃出字改后期叠字 → 6. 换参考图（换**构图最接近**的，不是最清晰的）。
