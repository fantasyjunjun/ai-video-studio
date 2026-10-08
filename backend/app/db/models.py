"""数据模型（SQLAlchemy 2.0）。

存储原则：
  - 元数据进 SQLite；
  - **图 / 视频 / 音频等大文件绝不进库**，只存路径 + 校验和；
  - 资产检索走 SQLite FTS5 虚表（迁移脚本里建）；
  - SQLAlchemy 方言无关，团队版切 PostgreSQL 只需改连接串。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


def _now() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class Product(Base):
    """商品（电商素材库）。

    字段模型**对齐 ClipForge 商品库**（用户 2026-09-28 定案，属"完全替换"）：
    名称 / 品类 / 卖点描述 / 多图 / 价格 / 目标人群。

    被移除的字段： `notes_level`、`notes_card_path`、`notes_json`。
    它们是"香调事实分级（铁律 15）"的载体 —— 那条铁律要求 C 级无来源事实
    不得写进文案，由 A/B 两级白名单放行。用户明确选择按 ClipForge 的模型
    完全替换，**该门禁因此停用**：现在商品事实不再分级，卖点描述一律可写。
    这是有意识的取舍（代价：失去"无来源事实拦截"，改由人工在表单里把关），
    不要把这条当 bug 修回去 —— 恢复门禁需要先与用户确认。

    `code` 保留，但**改为服务端自动生成**（P01、P02…）：
    它在提示词里充当产品锚点（跨镜逐字复用），属内部标识，不出现在表单上。
    """

    __tablename__ = "product"

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True)  # 服务端生成：P01、P02…
    name = Column(String)
    category = Column(String, default="other")  # beauty/food/home/fashion/tech/other
    description = Column(Text)                  # 卖点描述（写进提示词的事实来源）
    images = Column(JSON, default=list)         # 商品图 URL 列表（≤5，落本地磁盘）
    # 每张图的用途标记，与 images 下标对齐：[{"role": bottle|infographic|poster|other}]。
    # 只有 bottle（瓶身照）进 i2v 参考图 —— 信息图/海报喂进去会把版式、第二只
    # 型号瓶复刻进画面（R-16 实测穿帮）。缺省按 bottle，老数据行为不变。
    image_meta = Column(JSON, default=list)
    price = Column(String)                      # 自由文本："129" / "¥199"
    target_audience = Column(String)            # 目标人群
    brand = Column(String, default="")
    tags = Column(String, default="")
    created_at = Column(DateTime, default=_now)


class Talent(Base):
    """主播（出镜人物）。命名避开 `model`，以免与 ML 模型混淆。

    主播的产出链条是「基本信息 → 形象提示词 → 参考图」三段，字段也照此分：

        必填基本信息  name / age / gender / nationality
                     —— 年龄·性别·国籍是选角的三个硬维度，缺一个模型就只能瞎猜族裔，
                        后面每一版提示词都会漂；所以接口层强校验必填。
        形象提示词    appearance（+ appearance_source 记录它是手写的还是模型生成的）
                     —— 跨镜逐字复用的 [REF] 锚点，也是文生图时的人物描述。
        参考图        images（front / threeview / sheet / variant / poster）
                     —— 正面图与三视图由文生图产出，也可直接导入已有图片。

    **形象分三处存**（`pipeline/talent_spec.py` 是全部词表的唯一事实来源）：

        appearance   身份层 + 造型层拼出来的那段**文字**。继续用自由文本，是因为
                     下游（分镜、九宫格、成片）全都在读它，改成结构化要牵动整条链路。
        spec         JSON：十六个维度的选中值（服装 / 妆容 / 发型 / 配饰 / 场景 /
                     光线 / 姿势 / 构图 / 色调 / 风格 …）。它是 `appearance` 的**来源**，
                     `appearance` 是它的产物 —— 分开存才能做到"改一个维度不影响其余"。
        preset       最后套过的人设预设，作用仅是把界面回填到用户上次的选择。

    **为什么不按层拆成三列**：层是词表的组织方式（`talent_spec.LAYER_KEYS`），
    而不是数据的固有属性 —— 哪天才发现"表情其实该归画面层"，拆成列就得迁移。
    存一份完整的 `spec`，层划分留给运行时代码去解释。
    """

    __tablename__ = "talent"

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True)  # 服务端生成：M01、M02…
    name = Column(String)
    age = Column(Integer)                      # 必填：年龄段决定脸与体态的生成先验
    gender = Column(String, default="")        # 必填：female / male / neutral
    nationality = Column(String, default="")   # 必填：东亚 / 东南亚 / 欧美 …
    description = Column(String, default="")   # 一句话人物设定
    appearance = Column(Text)                  # 人物形象提示词（[REF] 锚点，≤35 词为宜）
    appearance_source = Column(String, default="")  # manual / llm（提示词是谁写的）
    spec = Column(JSON, default=dict)          # 十六维度的选中值，是 appearance 的来源
    preset = Column(String, default="")        # 最后套用的人设预设（界面回填用）
    voice_style = Column(String, default="")   # 声线风格
    # R-32：`is_default`（默认主播）已删除 —— 全后端核实它从未被生成链路读取
    # （出片/生文/出图一律走 `project.talent_id`），只做列表排序与星标，
    # 属于纯装饰字段。删列见迁移 0009。
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)

    images = relationship("TalentImage", back_populates="talent", cascade="all, delete")


class TalentImage(Base):
    """主播参考图。

    kind 取值：
      front / threeview / variant —— 手工登记的参考图（换装变体作 variant 行，不新建 code）
      sheet                       —— **2x2 四视图定妆照**（一次生成四机位同一人），
                                     作跨镜人脸锚点，是主播卡片上展示的那张
    """

    __tablename__ = "talent_image"

    id = Column(Integer, primary_key=True)
    talent_id = Column(Integer, ForeignKey("talent.id"))
    kind = Column(String)  # front / threeview / sheet / variant / poster
    #   （**登记**白名单 = pipeline/sheet.py 的 REGISTERABLE_TALENT_IMAGE_KINDS，
    #     它比**出图**用的 TALENT_IMAGE_KINDS 多一个只能导入的 `variant`；
    #     poster 是带场景的成品图，不进 i2v 参考图，见 services/refs.py）
    variant_name = Column(String, default="")  # blackgown 等
    path = Column(String)
    checksum = Column(String, default="")
    created_at = Column(DateTime, default=_now)

    talent = relationship("Talent", back_populates="images")


class Project(Base):
    """项目：记录当时所用供应商，便于复现。"""

    __tablename__ = "project"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    language = Column(String, default="pt-BR")
    talent_id = Column(Integer, ForeignKey("talent.id"), nullable=True)
    llm_provider = Column(String)
    image_provider = Column(String)
    video_provider = Column(String)
    status = Column(String, default="draft")
    cost_cny = Column(Float, default=0.0)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    shots = relationship("Shot", back_populates="project", cascade="all, delete")


class ProjectProduct(Base):
    """项目 ↔ 产品（支持双型号片 P01+P02）。"""

    __tablename__ = "project_product"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("project.id"))
    product_id = Column(Integer, ForeignKey("product.id"))
    role = Column(String, default="")  # hero_day / hero_night


class Shot(Base):
    """分镜。单镜可独立重跑（status 按镜存储）。"""

    __tablename__ = "shot"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("project.id"))
    idx = Column(Integer)
    code = Column(String)  # A1 / B1 / B1b
    prompt_en = Column(Text)
    prompt_zh = Column(Text)
    duration_sec = Column(Float)
    target_frames = Column(Integer)
    status = Column(String, default="draft")
    lint_score = Column(Float)
    lint_report = Column(JSON, default=dict)
    onset_sec = Column(Float)  # 实测的雾/喷雾起点，用于音效落位
    video_path = Column(String)
    seed = Column(Integer)
    # ★R-33 逐镜商品分配（多商品项目的硬要求）：本镜画面里出现的商品 code，
    # 如 `["P01"]`。出片时**只有这些商品的图**会挂进该镜的 ref_images
    # （services/refs.py），而不是把项目里所有商品的图都塞给每一镜 ——
    # 实测那是"两瓶同框"的根源，且产品主导镜的首帧参考图会取错型号。
    # 空列表 = 脚本没写 → 回退旧行为（挂项目全部商品），不阻断。
    products = Column(JSON, default=list)
    # ★R-33 章节级造型：本镜的服装/发型覆盖（英文短语），空 = 沿用主播锚点。
    # 身份锚点（脸/体型/族裔）永远来自主播参考图，这里只换造型，用来让
    # "白天 / 夜晚"这类章节在服装层也能区分开。
    talent_look = Column(String, default="")
    created_at = Column(DateTime, default=_now)

    project = relationship("Project", back_populates="shots")


class AudioTrack(Base):
    """音轨产物登记（念白 / 垫乐 / 音效 / 时间轴 / 成片）。

    大文件照例**只存路径**，本表负责"这一版用了哪条音轨、参数是什么"，
    便于复现与回溯（比如"为什么这条念白听起来发赶" → 查 meta.tempo）。
    """

    __tablename__ = "audio_track"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=True)
    kind = Column(String)  # voice / bgm / sfx / timeline / final
    path = Column(String)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)


class Asset(Base):
    """资产：只存路径 + 校验和，大文件留磁盘。"""

    __tablename__ = "asset"

    id = Column(Integer, primary_key=True)
    kind = Column(String)  # image / video / audio / report
    path = Column(String)
    checksum = Column(String, default="")
    project_id = Column(Integer, ForeignKey("project.id"), nullable=True)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)


class PromptRun(Base):
    """每次提示词生成留痕，便于回溯与成本统计。"""

    __tablename__ = "prompt_run"

    id = Column(Integer, primary_key=True)
    shot_id = Column(Integer, ForeignKey("shot.id"), nullable=True)
    provider_id = Column(String)
    model = Column(String)
    system_prompt = Column(Text)
    user_prompt = Column(Text)
    response = Column(Text)
    lint_score = Column(Float)
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


class ReverseReport(Base):
    """参考片反推产物。"""

    __tablename__ = "reverse_report"

    id = Column(Integer, primary_key=True)
    source_path = Column(String)
    spec_json = Column(JSON, default=dict)  # 分辨率/帧率/时长
    structure_json = Column(JSON, default=dict)  # 逐镜结构
    findings = Column(Text)  # 可迁移 / 须改造 / 不可采纳
    created_at = Column(DateTime, default=_now)


class Setting(Base):
    """全局键值设置（轻量 KV 表）。

    只放"一个值、全局生效、UI 可改"的小配置，例如 `reverse_vision_model`
    （反推看图的视觉模型覆盖，留空=用当前 LLM 模型）。**不放密钥**——
    密钥仍走 OS 凭据库（见 config.resolve_keys）；这里只是非敏感的开关/覆盖值。
    与 providers.yaml 解耦：这类设置无需改 YAML、也不该进配置文件。
    """

    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, default="")


class RenderJob(Base):
    """单条生成任务（图 / 视频）。

    与 shot 分离：同一镜可以多次重跑（换 seed、换供应商），
    每次都留痕，对应"只重跑某一镜"的局部返工场景。
    """

    __tablename__ = "render_job"

    id = Column(Integer, primary_key=True)
    shot_id = Column(Integer, ForeignKey("shot.id"), nullable=True)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=True)
    kind = Column(String)  # image / video
    provider_id = Column(String)
    provider_job_id = Column(String, default="")
    status = Column(String, default="queued")  # queued/running/succeeded/failed
    progress = Column(Float, default=0.0)
    message = Column(Text, default="")
    params = Column(JSON, default=dict)  # duration/resolution/seed/ref_images
    output_path = Column(String)
    checksum = Column(String, default="")
    cost_cny = Column(Float, default=0.0)
    dry_run = Column(Integer, default=0)
    error = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class RenderTask(Base):
    """渲染任务**台账**（P-2）。

    与 `RenderJob` 的区别：`RenderJob` 记录"这一镜要出什么、当前进度"，
    台账记录的是"这次提交在厂商那边的凭据"。

    存在的唯一理由：出片是**「提交即计费」的非幂等**操作。如果进程在
    「请求已发出、task_id 还没拿到」之间崩溃，钱可能已经花了、任务却成了孤儿，
    而 `RenderJob.provider_job_id` 此时还是空 —— 无从对账。

    台账把状态机显式化：
        submitting → submitted → running → succeeded / failed
                      └──────────────→ unknown（进程中断，无法对账）
    `client_ref` 是本地生成的关联串（提交前就有），用于"我们确实发过这一单"的审计。
    """

    __tablename__ = "render_task"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("render_job.id"), nullable=True)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=True)
    kind = Column(String)  # image / video
    provider_id = Column(String)
    client_ref = Column(String, default="")        # 本地关联 id（提交前生成）
    provider_task_id = Column(String, default="")  # 厂商 task id
    status = Column(String, default="submitting")  # submitting/submitted/running/succeeded/failed/unknown
    attempt = Column(Integer, default=1)
    cost_cny = Column(Float, default=0.0)
    submitted_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    message = Column(Text, default="")
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ImageHostLease(Base):
    """临时图床租约登记。

    AutoDL 之类的平台要求 ref_image 是**公网可达 URL**，
    所以出片前要把本地参考图发到图床，出片后必须下线。
    这张表就是"别忘了下线"的记账本：租约没关闭 = 有资产还挂在网上。
    """

    __tablename__ = "image_host_lease"

    id = Column(Integer, primary_key=True)
    host = Column(String)  # local_static / manual_relay / ...
    root_url = Column(String)
    serve_root = Column(String, default="")
    project_id = Column(Integer, ForeignKey("project.id"), nullable=True)
    published = Column(JSON, default=list)  # 已发布的文件名清单
    # 发布目录。并发 job 的参考图常同名（都叫 m02_front.jpg），
    # 靠 namespace 才能区分"谁的图"，避免提前下线别人的图。
    namespace = Column(String, default="")
    is_offline = Column(Integer, default=0)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    offline_at = Column(DateTime, nullable=True)


class BatchRun(Base):
    """批量变体运行（P5）。

    一份 manifest = 一条 campaign，把"视觉层出片 × 语言变体换念白"的矩阵
    固化成**可重跑的工作流源**（改一行 re-run，而不是靠人记得之前点了什么）。
    """

    __tablename__ = "batch_run"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=True)
    manifest_json = Column(JSON, default=dict)
    plan_json = Column(JSON, default=dict)
    status = Column(String, default="draft")  # draft/planned/running/done/failed
    cost_cny = Column(Float, default=0.0)
    work_dir = Column(String, default="")
    message = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    tasks = relationship("BatchTask", back_populates="run", cascade="all, delete")


class BatchTask(Base):
    """批量里的**单个去重单元**。

    一条 task 对应一次真实工作：出片（VP01/A1）、念白（pt-BR）、BGM、
    SFX、混流（VP01×pt-BR）。之所以单独建表而不是只记日志：
    断点续跑要靠它判断"这一步已经做过了，别再花钱"。
    """

    __tablename__ = "batch_task"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("batch_run.id"))
    kind = Column(String)  # render / silent / narration / bgm / sfx / mux
    key = Column(String)  # "VP01/A1" / "pt-BR" / "VP01×pt-BR"
    status = Column(String, default="pending")  # pending/running/succeeded/failed/skipped
    output_path = Column(String, default="")
    message = Column(Text, default="")
    cost_cny = Column(Float, default=0.0)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    run = relationship("BatchRun", back_populates="tasks")


class ProduceRun(Base):
    """一键出片运行：脚本 → 落镜 → 逐镜出片 → 15s 成片。

    与 `batch_run` 的区别不是数量而是**形态**：批量跑的是"一份 manifest × N 个变体"，
    这里跑的是**单个项目的一条完整成片**，中途要花 N 笔钱、耗时可达十几分钟。

    为什么非要落库：没有这条记录，前端就只能对着一个没有进度的按钮发呆，
    失败时也无从知道卡在哪一步、哪一镜、钱花在哪。`steps` 记逐步骤状态，
    `plan` 是花钱前那份规划快照（事后对账用），`report` 是分步产出报告。
    """

    __tablename__ = "produce_run"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=True)
    status = Column(String, default="queued")   # queued / running / done / failed
    stage = Column(String, default="script")    # script / render / compose / done
    message = Column(Text, default="")
    # 这一版成片用的脚本素材（点「出片」时若带了脚本，就是它）
    script_asset_id = Column(Integer, ForeignKey("asset.id"), nullable=True)
    plan = Column(JSON, default=dict)           # 规划快照（镜数/帧数/念白/预估花费）
    steps = Column(JSON, default=list)          # [{key,label,status,message,at}]
    report = Column(JSON, default=dict)         # 分步产出报告 + final_path
    cost_estimate_cny = Column(Float, default=0.0)
    cost_cny = Column(Float, default=0.0)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class CostRecord(Base):
    """成本追踪。"""

    __tablename__ = "cost_record"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=True)
    kind = Column(String)  # llm / image / video
    provider_id = Column(String)
    amount_cny = Column(Float, default=0.0)
    quantity = Column(Float, default=0.0)  # tokens 或秒数
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
