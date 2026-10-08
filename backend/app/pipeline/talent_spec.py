"""主播形象规范：三层维度词表 + 确定性拼装。

## 为什么要有这个文件

之前「生成人物形象提示词」只有三个输入：年龄、性别、国籍。剩下的全交给文本大模型
自由扩写，于是：

- **服装、场景、光线根本不会出现**。`sheet.py` 的旧系统提示词里明写着
  `Never describe clothing, scene, lighting, camera`——那条禁令为了保护跨镜锚点
  `[REF]` 不被污染，代价把"怎么画"这一层整个抹掉了；
- **画面参数是写死的**：场景永远是"浅灰纯色摄影棚背景"，姿势永远是"站姿双臂下垂"。
  看起来像模型随机发挥，实际是被钉死在一个最无聊的默认值上；
- **提示词是概率性的**，同一份输入点两次可能得到两套完全不同的人，用户无从预期。

这个文件把那些看不见的变量**显式化**：

    身份层 identity  这个人是谁（脸型/肤色/身材/气质/神情）—— 跨图跨镜必须一致
    造型层 styling   这一身穿什么（服装/妆容/发型/发色/配饰）—— 换装即新变体
    画面层 render    这一次怎么拍（场景/光线/姿势/构图/色调/风格）—— 每张图可不同

每一项都是**枚举**，每个枚举值带着预先写好的中英双语「可画片段」。拼装因此是
**确定性的**：用户勾了什么，提示词里就一定有那句话；模型没有自由发挥的空间，
也就没有漂移的空间。文本大模型的职责从"发明"降级为"把已有片段组织成通顺的话"。

## 关于「画质」和「负面提示」

这两项用户表格里列了，这里**故意不做成选项**。原因：它们是所有图都要的常量，
做成选项只会让人漏勾——漏了就出低质图。参看本文件末尾的 `QUALITY_BASE` 与
`NEGATIVE_BASE`，由拼装器无条件带上。

## 关于三类图的差异

`render` 层（场景/光线/姿势/构图/色调/风格）只对**氛围图 poster** 生效。
正面图 / 三视图 / 定妆照是给下游当作 i2v 参考图用的，一旦写了"咖啡厅""逆光"，
那张背景会被后续每一个镜头原样复刻进成片——这是 `Dim.reference_safe` 为 False
的全部理由。想拍带场景的成品图，请生成氛围图。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Opt:
    """一个可选项。

    `zh`  —— 界面上显示的短标签
    `zhp` —— 中文提示词片段（要能直接接进句子里）
    `en`  —— 英文提示词片段。生图模型对英文的遵循度更高，这是主力。
    """

    value: str
    zh: str
    zhp: str
    en: str


@dataclass(frozen=True)
class Dim:
    """一个维度。

    `layer`           identity / styling / render
    `reference_safe`  是否对参考图类（正面图 / 三视图 / 定妆照）生效。
                      False 的维度只对氛围图生效——详见文件头。
    `lead`            拼装时的引出词（英文），空串表示直接拼接。
    `lead_zh`         拼装时的引出词（中文）。
    """

    key: str
    layer: str
    zh: str
    lead: str = ""
    lead_zh: str = ""
    reference_safe: bool = True
    options: Tuple[Opt, ...] = ()


def _o(value: str, zh: str, zhp: str, en: str) -> Opt:
    return Opt(value, zh, zhp, en)


# ---------------------------------------------------------------- 基本信息枚举
# 性别与国籍的**唯一事实来源**在这里而不是 api/assets.py —— 因为拼装需要国籍的
# 英文写法，枚举和它的英文映射必须相邻才有可能同步。
# 前端下拉、后端校验、`profile_lead` 三处都读这一份。

GENDERS = ("female", "male", "neutral")
GENDER_LABELS = {"female": "女", "male": "男", "neutral": "中性"}

# 国籍在界面上显示中文，拼进英文提示词却需要模型看得懂的形容词。
# 一律用 "xxx features" 这种选角用语，而不是直接指认种族 —— 描述外貌特征
# 是影视选角的标准写法，也能避免模型把它当作某种状态或身份去发挥。
NATIONALITY_EN = {
    "中国": "East Asian", "日本": "East Asian", "韩国": "East Asian",
    "东南亚": "Southeast Asian", "南亚": "South Asian", "中东": "Middle Eastern",
    "欧美": "European", "拉美": "Latin American", "非洲": "African", "混血": "mixed-ethnicity",
}
NATIONALITIES = tuple(NATIONALITY_EN.keys())


# ---------------------------------------------------------------- 身份层
# 这些原本是模型自由扩写的内容，也是"每次生成都不像同一个人"的主因。
# 全部收成枚举后，跨图一致性不再依赖模型的运气。

IDENTITY_DIMS: Tuple[Dim, ...] = (
    Dim("face_shape", "identity", "脸型", "", "", True, (
        _o("oval", "鹅蛋脸", "柔和的鹅蛋脸，下颌线流畅内收",
           "a soft oval face with a smooth tapered jawline"),
        _o("round", "圆脸", "圆润的脸型，脸颊饱满，下巴柔和",
           "a round face with full cheeks and a soft chin"),
        _o("heart", "瓜子脸", "心形瓜子脸，额头略宽、下巴尖而窄",
           "a heart-shaped face with a narrow chin and a slightly wider forehead"),
        _o("square", "方脸", "轮廓分明的方脸，下颌骨清晰、颧骨平直",
           "a square face with a defined jawline and flat cheekbones"),
        _o("oblong", "长脸", "偏长的脸型，下巴线条修长清晰",
           "an oblong face with a gently elongated chin"),
        _o("diamond", "菱形脸", "菱形脸，颧骨略高，下颌收窄",
           "a diamond face with high cheekbones tapering to a narrow chin"),
    )),
    Dim("skin_tone", "identity", "肤色", "", "", True, (
        _o("porcelain", "白皙冷调", "瓷白偏冷的肤色，肤质细腻",
           "porcelain fair skin with a cool undertone"),
        _o("fair-neutral", "自然偏白", "自然偏白的肤色，色调中性",
           "fair skin with a neutral undertone"),
        _o("light-warm", "健康暖白", "暖白肤色，带健康自然的光泽",
           "light warm-toned skin with a natural healthy glow"),
        _o("wheat", "小麦色", "小麦色肌肤，偏暖、干净均匀",
           "lightly tanned wheat-toned skin"),
        _o("deep", "深棕肤色", "深棕肤色，质感均匀细腻",
           "rich deep-brown skin with an even tone"),
    )),
    Dim("body_build", "identity", "身材", "", "", True, (
        _o("slim", "纤瘦", "纤瘦单薄的身形，肩线窄而柔和",
           "a slim slender build with softly narrow shoulders"),
        _o("tall-slim", "高挑纤长", "高挑纤长的身形，四肢修长",
           "a tall slim build with long limbs"),
        _o("average", "匀称自然", "匀称自然的身形，比例协调",
           "a balanced average build with natural proportions"),
        _o("curvy", "丰满曲线", "丰满有曲线的身形，腰臀线条明显",
           "a curvy full-figured build with a defined waist and hip line"),
        _o("athletic", "健美紧致", "紧致健美的身形，肌肉线条轻透",
           "a toned athletic build with lean visible muscle lines"),
    )),
    # 气质是最容易被写成 beautiful / elegant 这类废话的地方。这里每一项都落到
    # 眼睛、眉、嘴这类**画得出来的**部位上。
    Dim("temperament", "identity", "气质", "", "", True, (
        _o("warm", "温柔亲和", "温柔亲和的气质，眼神柔和、嘴角放松",
           "a warm approachable presence with soft friendly eyes and relaxed lips"),
        _o("polished", "干练专业", "干练沉稳的气质，眼神坚定、姿态从容",
           "a polished composed presence with steady confident eyes and an upright calm posture"),
        _o("cool", "高级冷感", "冷感疏离的高级气质，目光平直、不露情绪",
           "a cool restrained editorial presence with a level distant gaze and minimal affect"),
        _o("fresh", "明朗元气", "明朗元气的气质，眼神清亮、表情松弛",
           "a fresh lively presence with bright open eyes and an easy open manner"),
        _o("gentle", "安静内敛", "安静内敛的气质，目光低垂、举止轻缓",
           "a gentle quiet presence with lowered relaxed eyes and unhurried manner"),
        _o("confident", "飒爽利落", "飒爽利落的气质，眉眼有神、下颌微抬",
           "a crisp self-assured presence with animated brows and a slightly lifted chin"),
    )),
    # 表情放身份层而不是画面层：它是"这个人一贯的神情"，跨图保持一致才不会在
    # 参考图之间漂。画面层负责的是灯光和机位这类纯布景。
    Dim("expression", "identity", "表情", "", "", True, (
        _o("neutral", "平静自然", "平静自然的表情，双唇放松微合",
           "a calm neutral expression with softly closed relaxed lips"),
        _o("soft-smile", "温柔微笑", "嘴角微微上扬的温柔浅笑，不露齿",
           "a soft closed-lip smile with slightly raised cheeks, no teeth"),
        _o("bright-smile", "明朗笑容", "明朗的笑容，自然露出上排牙齿",
           "a bright natural smile showing a hint of upper teeth"),
        _o("focused", "专注认真", "专注认真的表情，双眉平展、视线集中",
           "a focused thoughtful expression with level brows and a concentrated gaze"),
        _o("aloof", "冷峻疏离", "冷峻疏离的表情，视线平直、不做表情",
           "a composed aloof expression with a level gaze and no visible smile"),
    )),
)

# ---------------------------------------------------------------- 造型层
# 用户表格里"模型会随机发挥，可能出运动服、礼服、古装"说的就是这一层。
# 服装的每一个值都必须自带**款式 + 颜色 + 材质**三件套，缺一项就会被自由发挥。

STYLING_DIMS: Tuple[Dim, ...] = (
    Dim("outfit", "styling", "服装", "wearing ", "穿着", True, (
        _o("white-shirt", "白衬衫+西裤", "象牙白棉质衬衫扎进剪裁合身的西裤",
           "a crisp ivory cotton shirt tucked into tailored trousers"),
        _o("blazer", "西装外套", "炭灰羊毛西装外套内搭白色丝绸吊带与西裤",
           "a charcoal wool blazer over a white silk camisole and tailored trousers"),
        _o("knit-casual", "针织衫日常", "燕麦色羊毛针织衫搭配直筒长裤",
           "a soft oatmeal wool knit sweater with straight-leg trousers"),
        _o("slip-dress", "缎面吊带裙", "香槟色缎面吊带长裙，裙长及膝下",
           "a champagne satin slip dress falling just below the knee"),
        _o("silk-blouse", "丝绸衬衫", "灰玫瑰色丝绸衬衫，垂坠柔软、珍珠扣",
           "a muted-rose silk blouse with soft drape and small pearl buttons"),
        _o("turtleneck", "高领针织", "细罗纹黑色高领衫搭配阔腿羊毛长裤",
           "a fine-ribbed black turtleneck with wide-leg wool trousers"),
        _o("casual-tee", "T恤+牛仔", "宽松白色亚麻T恤与浅色水洗直筒牛仔裤",
           "a relaxed white linen tee with light-wash straight jeans"),
        _o("denim-jacket", "牛仔外套", "浅蓝牛仔外套内搭罗纹白色背心与直筒牛仔裤",
           "a light-blue denim jacket over a ribbed white tank and straight jeans"),
        _o("hoodie", "连帽卫衣", "oversize 灰褐色连帽卫衣搭配修身运动长裤",
           "an oversized heather-grey cotton hoodie with slim joggers"),
        _o("activewear", "运动套装", "黑色罗纹运动套装，短款拉链外套",
           "a black ribbed activewear set with a cropped zip jacket"),
        _o("velvet-gown", "丝绒礼服", "深祖母绿丝绒晚礼服，一字领干净利落",
           "a deep-emerald velvet evening gown with a clean bateau neckline"),
        _o("retro-dress", "复古衬衫裙", "锈棕色复古衬衫裙，收腰系带",
           "a rust-brown retro shirt dress with a cinched waist belt"),
    )),
    Dim("makeup", "styling", "妆容", "", "", True, (
        _o("bare", "素颜裸妆", "近乎素颜，仅修饰眉形、唇部自然",
           "barely-there makeup with groomed brows and natural lips"),
        _o("natural", "自然淡妆", "自然淡妆，中性眼影与清透唇蜜",
           "soft natural makeup with neutral lids and a sheer balm lip"),
        _o("commuter", "通勤妆", "干净通勤妆，柔雾底妆、眉形清晰、豆沙唇",
           "clean commuter makeup with soft matte skin, defined brows and a muted rose lip"),
        _o("glam", "精致全妆", "精致全妆，修容明确、眼影晕染、眼线清晰、缎面唇",
           "full glam makeup with contoured cheeks, blended smoky lids, defined liner and a satin lip"),
        _o("red-lip", "红唇复古", "复古妆，上扬眼线与哑光正红唇",
           "classic retro makeup with winged liner and a bold matte red lip"),
        _o("fresh-sport", "运动清透", "清透运动妆，水光底妆、眉目干净",
           "fresh minimal makeup with a dewy finish and clear brows"),
    )),
    # 发型：**长度 + 分缝 + 刘海 + 卷直**四项齐全才叫具体。"齐肩直发"仍然
    # 有无穷多变体，这也是用户表格里点名的一条。
    # lead 是 `hair styled `，所以每个 en 都以 `in ...` 开头，避免"hair ... hair"重复。
    Dim("hair_detail", "styling", "发型细节", "hair styled ", "发型为", True, (
        _o("center-long-straight", "中分长直发", "中分长直发，垂过肩背",
           "in a center-parted long straight cut falling past the shoulders"),
        _o("side-long-wave", "偏分长卷发", "偏分长发，发尾带柔和的大波浪",
           "in a side-parted long cut finishing in soft loose waves"),
        _o("shoulder-inward", "齐肩内扣", "齐肩中长发，发尾微微内扣",
           "in a shoulder-length cut with gently inward-curled ends"),
        _o("bob-curtain", "短波波头+八字刘海", "及下巴短波波头，带柔和八字刘海",
           "in a chin-length bob with a soft curtain fringe"),
        _o("low-bun", "低发髻", "服帖的低发髻，发际线干净梳起",
           "in a sleek low bun with the hairline cleanly combed back"),
        _o("low-pony", "低马尾", "颈后低马尾，两侧留几缕碎发修饰脸型",
           "in a low ponytail at the nape with a few loose face-framing strands"),
        _o("high-pony", "高马尾", "高马尾，头顶服帖、发尾自然摆动",
           "in a high ponytail with a smooth crown and natural swing"),
        _o("messy-bun", "慵懒丸子头", "慵懒丸子头，鬓角留有自然碎发",
           "in a relaxed messy bun with soft loose wisps around the face"),
        _o("short-pixie", "精灵短发", "精灵短发，层次纹理轻盈",
           "in a short pixie cut with soft layered texture"),
    )),
    Dim("hair_color", "styling", "发色", "", "发色为", True, (
        _o("black", "自然黑", "自然黑色",
           "natural black hair"),
        _o("dark-brown", "深棕", "深棕色",
           "dark brown hair"),
        _o("chestnut", "栗棕", "温暖的栗棕色",
           "warm chestnut brown hair"),
        _o("caramel", "焦糖棕", "焦糖棕色，带柔和挑染",
           "caramel brown hair with soft highlights"),
        _o("ash-brown", "冷调灰棕", "冷调灰棕色",
           "cool ash-brown hair"),
        _o("auburn", "酒红棕", "深酒红色",
           "deep auburn hair"),
        _o("honey-blonde", "蜜金棕", "柔和蜜金色",
           "soft honey-blonde hair"),
    )),
    Dim("accessory", "styling", "配饰", "wearing ", "佩戴", True, (
        _o("none", "不戴", "", ""),
        _o("thin-glasses", "细框眼镜", "细金边矩形眼镜",
           "thin gold-rimmed rectangular glasses"),
        _o("pearl-earrings", "珍珠耳钉", "小巧的珍珠耳钉",
           "small pearl stud earrings"),
        _o("hoop-earrings", "金色小圆环耳环", "细金色小圆环耳环",
           "small gold hoop earrings"),
        _o("drop-earrings", "单石耳坠", "单颗宝石的垂坠耳环",
           "elegant drop earrings with a single stone"),
        _o("gold-necklace", "细金项链", "纤细的金色吊坠项链",
           "a delicate thin gold pendant necklace"),
        _o("silk-scarf", "丝质小方巾", "松松系着的细丝质小方巾",
           "a slim silk neckerchief loosely knotted"),
        _o("watch", "细表带腕表", "左腕一枚细表带不锈钢腕表",
           "a slim stainless-steel watch on the left wrist"),
    )),
)

# ---------------------------------------------------------------- 画面层
# 全部 reference_safe=False：只对氛围图 poster 生效。理由见文件头。

RENDER_DIMS: Tuple[Dim, ...] = (
    # 场景值一律带 "softly blurred / out of focus"，避免背景抢主体。
    # 这是参考图污染问题的另一面：背景即使只是辅助，也必须是虚的。
    Dim("scene", "render", "场景背景", "set in ", "场景为", False, (
        _o("studio-plain", "纯色影棚", "纯浅灰无缝影棚背景",
           "a plain seamless light-grey studio backdrop"),
        _o("minimal-living", "简约客厅", "暖调简约客厅，亚麻沙发与一株绿植，背景虚化",
           "a minimal warm-toned living room with a linen sofa and one potted plant, softly blurred"),
        _o("study", "书房", "安静书房，通顶书架与木质书桌，背景虚化",
           "a quiet study with floor-to-ceiling bookshelves and a wooden desk, softly blurred"),
        _o("cafe", "咖啡厅", "明亮咖啡厅角落，木质餐桌与窗光，背景虚化",
           "a bright corner cafe with warm wood tables and window light, softly blurred"),
        _o("window", "落地窗旁", "明亮的室内，位于高垂帘落地窗旁，窗外城市景色柔化",
           "a bright interior beside a tall sheer-curtain window with a soft city view behind"),
        _o("street", "街景", "午后柔和虚化的林荫街道",
           "a soft-focus tree-lined street in late afternoon"),
        _o("garden", "绿植花园", "绿意浓郁的花园，枝叶虚化成背景",
           "a shaded garden with dense green foliage behind"),
        _o("vanity", "梳妆台前", "明亮的梳妆台角落，圆镜与暖色灯泡",
           "a bright vanity corner with a round mirror and warm bulbs"),
    )),
    Dim("lighting", "render", "光线", "lit by ", "用光为", False, (
        _o("soft-even", "均匀柔光", "大面积正面柔光箱均匀布光，过渡柔和",
           "soft even key light from a large frontal softbox with gentle wrap"),
        _o("window-side", "侧窗自然光", "侧窗柔和自然光，明暗自然过渡",
           "soft directional daylight from a side window with natural falloff"),
        _o("rim-backlight", "逆光轮廓光", "暖色逆光勾出发丝与肩线轮廓，面部补光柔和",
           "warm rim backlight tracing a clean edge along hair and shoulders with a soft fill on the face"),
        _o("golden-hour", "黄昏暖光", "低角度黄昏暖光，肤温暖润、影子长而柔",
           "low golden-hour sunlight with warm skin tones and long soft shadows"),
        _o("cool-studio", "冷调棚灯", "冷调中性棚光，暗部界限清晰",
           "cool neutral studio light with a crisp defined shadow side"),
        # 布光不写参数等于没写。这条把光位、光比、衰减三者写全，
        # 模型才有可能真的打出硬光，而不是理解成"随便补个光"。
        _o("hard-dramatic", "单硬光戏剧光", "机位左 45 度单硬光、约 4:1 光比，暗部迅速衰减",
           "a single hard key at 45 degrees camera-left with a 4:1 ratio and deep shadow falloff"),
    )),
    # pose 的 en 自带动词（-ing 分词），不要加 lead，否则读不成句。
    Dim("pose", "render", "姿势", "", "", False, (
        _o("front-stand", "正面站姿", "正面站立，双臂自然垂于体侧",
           "standing front-facing with relaxed arms at the sides"),
        _o("three-quarter-stand", "侧身站姿", "放松的四分之三侧身站立，一侧肩在前",
           "standing in a relaxed three-quarter turn with one shoulder forward"),
        _o("side-stand", "侧面站姿", "侧面站立，重心放在后脚",
           "standing in profile with weight on the back foot"),
        _o("seated", "端坐", "端正坐于椅上，双手轻放于膝上",
           "seated upright with hands resting lightly in the lap"),
        _o("hold-product", "手持物件", "侧身站立，双手在胸前轻托一个小物件",
           "standing three-quarter with both hands cradling a small object at chest height"),
        _o("lean", "倚靠站姿", "轻倚墙面，一膝微屈、双臂放松",
           "leaning lightly against a wall with one knee bent and arms relaxed"),
        _o("walk", "行走姿态", "朝镜头行走中的自然摆臂姿态",
           "walking toward camera mid-stride with natural arm swing"),
    )),
    # 用户表格点了"焦距、景深"，这里直接写进每个值里，不必单独再开一维。
    Dim("shot", "render", "镜头构图", "", "", False, (
        _o("full", "全身", "竖幅全身构图，头顶到脚完整入画，约 35mm 视角、背景轻微虚化",
           "vertical full-body composition framing head to feet, roughly a 35mm look with the background slightly out of focus"),
        _o("three-quarter", "七分身", "竖幅七分身构图，取景到大腿中部以上",
           "vertical three-quarter composition framed from mid-thigh up"),
        _o("waist", "半身", "竖幅半身构图，取景到腰部以上",
           "vertical waist-up composition"),
        _o("bust", "胸像", "竖幅胸像构图，取景到胸部以上",
           "vertical bust portrait framed from mid-chest up, roughly an 85mm look"),
        _o("closeup", "面部特写", "肩部以上的面部特写，约 85mm、浅景深",
           "a tight facial close-up framed from the shoulders up at roughly 85mm with a shallow depth of field"),
    )),
    Dim("tone", "render", "色调", "graded with ", "", False, (
        _o("warm", "暖调", "柔和低对比的暖琥珀色调",
           "a warm amber-forward palette with soft low-contrast grading"),
        _o("cool", "冷调", "低饱和冷调，白色干净中性",
           "a cool desaturated palette with clean neutral whites"),
        _o("low-sat", "低饱和", "低饱和柔和色调，明暗对比轻微",
           "a low-saturation muted palette with gentle contrast"),
        _o("film", "胶片感", "暖调胶片色，暗部提亮、带细颗粒",
           "a warm film-inspired palette with lifted blacks and fine grain"),
        _o("monochrome-warm", "米杏单色", "接近单色的米杏色系，画面统一",
           "a warm near-monochrome palette dominated by beige and cream"),
    )),
    Dim("style", "render", "风格", "in the style of ", "风格为", False, (
        _o("photorealistic", "写实摄影", "干净写实的肖像摄影，肤质接近真实",
           "clean photorealistic portrait photography with true-to-life skin rendition"),
        _o("editorial", "杂志大片", "高级时装杂志摄影，细节锐利、用光精到",
           "high-end editorial fashion photography with crisp detail"),
        _o("cinematic", "电影感", "电影定帧质感，轻微暗角、留白考究",
           "cinematic still-frame photography with subtle vignetting and composed negative space"),
        _o("lifestyle", "商业生活化", "明亮通透的商业生活风摄影",
           "bright commercial lifestyle photography with an airy clean look"),
        _o("film-retro", "胶片复古", "柔和的复古胶片摄影，细颗粒与暖色光晕",
           "soft retro film photography with gentle grain and warm halation"),
    )),
)


# ---------------------------------------------------------------- 汇总索引

DIMS: Tuple[Dim, ...] = IDENTITY_DIMS + STYLING_DIMS + RENDER_DIMS
DIM_BY_KEY: Dict[str, Dim] = {d.key: d for d in DIMS}

LAYER_KEYS: Dict[str, Tuple[str, ...]] = {
    "identity": tuple(d.key for d in IDENTITY_DIMS),
    "styling": tuple(d.key for d in STYLING_DIMS),
    "render": tuple(d.key for d in RENDER_DIMS),
}

LAYER_LABELS: Dict[str, str] = {
    "identity": "身份（这个人是谁）",
    "styling": "造型（这一身穿什么）",
    "render": "画面（这一次怎么拍）",
}

OPT_BY_KEY: Dict[str, Dict[str, Opt]] = {
    d.key: {o.value: o for o in d.options} for d in DIMS}


# ---------------------------------------------------------------- 人设预设
# 14 个下拉让用户逐个选太重，实际使用几乎总是"先给个方向再微调"。
# 每个预设把全部维度一次填满，用户可以在界面上逐项覆盖。

@dataclass(frozen=True)
class Preset:
    value: str
    zh: str
    desc: str
    fits: str          # 适合什么品类，帮用户对号入座
    spec: Dict[str, str]


PRESETS: Tuple[Preset, ...] = (
    Preset("professional", "知性职场",
           "通勤装 + 干净妆面 + 侧窗自然光，可信度优先",
           "职场通勤品、办公好物、知识付费",
           {"face_shape": "oval", "skin_tone": "fair-neutral", "body_build": "average",
            "temperament": "polished", "expression": "focused",
            "outfit": "white-shirt", "makeup": "commuter",
            "hair_detail": "shoulder-inward", "hair_color": "dark-brown",
            "accessory": "thin-glasses",
            "scene": "study", "lighting": "window-side", "pose": "three-quarter-stand",
            "shot": "three-quarter", "tone": "cool", "style": "photorealistic"}),
    Preset("girl-next-door", "温柔邻家",
           "针织衫 + 素颜裸妆 + 暖调客厅，亲和力优先",
           "家居日用、个护、母婴亲子",
           {"face_shape": "round", "skin_tone": "light-warm", "body_build": "average",
            "temperament": "warm", "expression": "soft-smile",
            "outfit": "knit-casual", "makeup": "bare",
            "hair_detail": "center-long-straight", "hair_color": "black",
            "accessory": "none",
            "scene": "minimal-living", "lighting": "soft-even", "pose": "seated",
            "shot": "waist", "tone": "warm", "style": "lifestyle"}),
    Preset("cool-editorial", "高级冷感",
           "缎面裙 + 精致全妆 + 戏剧硬光，质感与距离感",
           "香水香氛、高端护肤、珠宝腕表",
           {"face_shape": "heart", "skin_tone": "porcelain", "body_build": "tall-slim",
            "temperament": "cool", "expression": "aloof",
            "outfit": "slip-dress", "makeup": "glam",
            "hair_detail": "low-bun", "hair_color": "black",
            "accessory": "drop-earrings",
            "scene": "studio-plain", "lighting": "hard-dramatic", "pose": "side-stand",
            "shot": "three-quarter", "tone": "cool", "style": "editorial"}),
    Preset("fresh-sporty", "阳光活力",
           "运动套装 + 清透妆 + 逆光/户外，元气与动感",
           "运动装备、健康食品、户外用品",
           {"face_shape": "oval", "skin_tone": "wheat", "body_build": "athletic",
            "temperament": "fresh", "expression": "bright-smile",
            "outfit": "activewear", "makeup": "fresh-sport",
            "hair_detail": "high-pony", "hair_color": "dark-brown",
            "accessory": "none",
            "scene": "street", "lighting": "rim-backlight", "pose": "walk",
            "shot": "full", "tone": "film", "style": "lifestyle"}),
    Preset("retro", "复古港风",
           "衬衫裙 + 红唇妆 + 暖黄光胶片感，怀旧与故事感",
           "彩妆口红、服饰配饰、文创礼盒",
           {"face_shape": "diamond", "skin_tone": "light-warm", "body_build": "slim",
            "temperament": "confident", "expression": "neutral",
            "outfit": "retro-dress", "makeup": "red-lip",
            "hair_detail": "shoulder-inward", "hair_color": "auburn",
            "accessory": "silk-scarf",
            "scene": "cafe", "lighting": "golden-hour", "pose": "lean",
            "shot": "bust", "tone": "film", "style": "film-retro"}),
    Preset("luxury", "轻奢贵妇",
           "丝绒礼服 + 精致全妆 + 柔光浓调，价格感与分量感",
           "高端礼赠、贵价护肤、展会主视觉",
           {"face_shape": "oval", "skin_tone": "porcelain", "body_build": "curvy",
            "temperament": "polished", "expression": "aloof",
            "outfit": "velvet-gown", "makeup": "glam",
            "hair_detail": "low-bun", "hair_color": "chestnut",
            "accessory": "drop-earrings",
            "scene": "window", "lighting": "rim-backlight", "pose": "front-stand",
            "shot": "full", "tone": "warm", "style": "cinematic"}),
)

PRESET_BY_VALUE: Dict[str, Preset] = {p.value: p for p in PRESETS}


# ---------------------------------------------------------------- 画质与负面
# **不做成选项**：这是每一张图都需要的东西，交给用户勾选只会漏。

QUALITY_BASE = {
    "en": "sharp focus, high-resolution rendering, clean detailed skin and fabric texture, "
          "natural optical depth of field",
    "zh": "对焦清晰，高分辨率渲染，皮肤与面料纹理细节干净，景深自然",
}

NEGATIVE_BASE = {
    "en": ("deformed hands, extra fingers, extra limbs, fused fingers, missing fingers, "
           "distorted face, asymmetric eyes, cross-eyed, lazy eye, blurry, low resolution, "
           "jpeg artifacts, watermark, caption, text overlay, logo, signature, border, frame, "
           "distorted background, warped perspective, cloned duplicate person, multiple people, "
           "over-smoothed plastic skin, overly retouched influencer face"),
    "zh": ("手部畸形、多余手指、多余肢体、手指粘连、缺指、面部扭曲、双眼不对称、斗鸡眼、"
           "模糊、低分辨率、JPEG 压缩痕、水印、字幕、贴片文字、logo、签名、边框、装饰框、"
           "背景扭曲、透视变形、同一个人重复出现、画面多人、过度磨皮的塑料脸、网红精修脸"),
}


# ---------------------------------------------------------------- 拼装


def normalize_spec(spec: Any) -> Dict[str, str]:
    """把外部传入的 spec 收敛成 {dim_key: opt_value}。

    只保留**词表里真实存在**的组合；非法值静默丢弃而不是报错 —— 词表升级后
    删掉某个旧选项时，老主播存下来的数据不该让整个接口炸掉。
    """
    out: Dict[str, str] = {}
    if not isinstance(spec, dict):
        return out
    for key, value in spec.items():
        if key not in DIM_BY_KEY:
            continue
        v = str(value or "").strip()
        if not v or v not in OPT_BY_KEY[key]:
            continue
        out[key] = v
    return out


def merge_spec(base: Any, override: Any) -> Dict[str, str]:
    """override 覆盖 base（后者为 None 或空串时不覆盖）。"""
    out = normalize_spec(base)
    for k, v in normalize_spec(override).items():
        out[k] = v
    return out


def _phrase(dim: Dim, opt: Opt, lang: str) -> str:
    if lang == "zh":
        # **不要回退到 `zh` 短标签**。像"不戴"这种选项的 zhp 故意留空表示"这一
        # 项不写进提示词"，一旦回退就会拼出"佩戴不戴"这种自相矛盾的话。
        body = opt.zhp
        lead = dim.lead_zh
        if not body:
            return ""
        return f"{lead}{body}" if lead else body
    body = opt.en
    if not body:
        return ""
    return f"{dim.lead}{body}" if dim.lead else body


def layer_phrases(spec: Dict[str, str], layer: str, lang: str = "en",
                  *, reference_safe_only: bool = False) -> List[str]:
    """取某一层的全部短语，按固定顺序。

    顺序是**有意固定的**：身份 → 造型 → 画面的顺序符合 prompt 的注意力分布
    （开头的 token 权重更高），每次都一样也便于 diff。
    """
    out: List[str] = []
    for key in LAYER_KEYS.get(layer, ()):
        dim = DIM_BY_KEY[key]
        if reference_safe_only and not dim.reference_safe:
            continue
        val = spec.get(key)
        if not val:
            continue
        opt = OPT_BY_KEY[key].get(val)
        if opt is None:
            continue
        p = _phrase(dim, opt, lang)
        if p:
            out.append(p)
    return out


def filter_spec_for_reference(spec: Dict[str, str]) -> Dict[str, str]:
    """剔掉 reference_safe=False 的维度。参考图（正面图/三视图/定妆照）专用。"""
    return {k: v for k, v in spec.items()
            if DIM_BY_KEY.get(k) is not None and DIM_BY_KEY[k].reference_safe}


def join_naturally(parts: List[str], lang: str) -> str:
    parts = [p.strip().rstrip(",，。.") for p in parts if p and p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if lang == "zh":
        return "，".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def profile_lead(profile: Dict[str, Any], lang: str = "en") -> str:
    """开头那句"29 岁的东亚女性"。

    年龄 / 性别 / 国籍是最强的生成先验，放在整段最前面而不是混在中间。
    国籍在库里存的是中文（`NATIONALITY_EN` 的键），这里才翻译成模型看得懂的
    英文形容词 —— 直接把"中国"拼进英文句子会得到 `a 29-year-old 中国 woman`
    这种既读不通、模型也无从遵循的东西。
    """
    age = profile.get("age")
    gender = str(profile.get("gender") or "").strip().lower()
    nation = str(profile.get("nationality") or "").strip()
    g_en, g_zh = {"female": ("woman", "女性"), "male": ("man", "男性"),
                  "neutral": ("androgynous person", "中性气质的人")}.get(
        gender, ("person", "人"))

    if lang == "zh":
        bits = []
        if age:
            bits.append(f"{int(age)}岁")
        if nation:
            bits.append(f"{nation}面孔")
        bits.append(g_zh)
        return "".join(bits)

    words = ["a"]
    if age:
        words.append(f"{int(age)}-year-old")
    adj = NATIONALITY_EN.get(nation, "")
    if adj:
        words.append(adj)
    words.append(g_en)
    return " ".join(words)


def person_sentence(profile: Dict[str, Any], spec: Dict[str, str],
                    lang: str = "en") -> str:
    """「人」这一句：基本信息 + 身份层 + 造型层。

    身份层用 `with` 链挂在人身上。造型层各自带引出词（wearing / hair styled / …），
    所以整段读起来是一句话，而不是十五个短语的罗列 —— 生图模型对连贯自然段的
    遵循度明显高于堆关键词。
    """
    head = profile_lead(profile, lang)
    ident = layer_phrases(spec, "identity", lang)
    styling = layer_phrases(spec, "styling", lang)
    if lang == "zh":
        body = "，" + "，".join(ident) if ident else ""
        tail = "，" + "，".join(styling) if styling else ""
        return f"{head}{body}{tail}".strip("，")
    out = head
    if ident:
        out += f" with {join_naturally(ident, lang)}"
    if styling:
        out += ", " + join_naturally(styling, lang)
    return out


def render_sentence(spec: Dict[str, str], lang: str = "en") -> str:
    """「画面」这一句：场景 / 光线 / 姿势 / 构图 / 色调 / 风格。

    独立成句而不并进描写人的那一句 —— 生图模型对"先写人、再写怎么拍"这种
    分段结构的遵循度明显高于一长串逗号。只有氛围图会用到它。
    """
    s = join_naturally(layer_phrases(spec, "render", lang), lang)
    # 它是接在句号后面的独立句子，首字母要大写（对中文无副作用）
    return s[:1].upper() + s[1:] if s else ""


def build_spec_sentence(profile: Dict[str, Any], spec: Dict[str, str],
                        lang: str = "en", *, include_render: bool = False) -> str:
    """拼装整段形象描述。

    `include_render=True` 时追加画面句（氛围图专用）。参考图一律传 False ——
    那一层的场景/布光会污染下游每一支成片，详见文件头。
    """
    s = person_sentence(profile, spec, lang)
    if not include_render:
        return s
    tail = render_sentence(spec, lang)
    if not tail or not s:
        return s or tail
    return f"{s}。{tail}" if lang == "zh" else f"{s}. {tail}"


# ---------------------------------------------------------------- 导出给前端


def dims_payload() -> List[dict]:
    """词表的 JSON 形态，供 `/api/talents/options` 返回。

    前端的下拉从这里读，避免同一份词表在前后端各写一遍（这是最容易漂的东西）。
    """
    return [
        {
            "key": d.key,
            "layer": d.layer,
            "label": d.zh,
            "reference_safe": d.reference_safe,
            "options": [{"value": o.value, "label": o.zh} for o in d.options],
        }
        for d in DIMS
    ]


def presets_payload() -> List[dict]:
    return [
        {"value": p.value, "label": p.zh, "desc": p.desc, "fits": p.fits, "spec": p.spec}
        for p in PRESETS
    ]


def layers_payload() -> List[dict]:
    return [{"key": k, "label": v, "dims": list(LAYER_KEYS[k])}
            for k, v in LAYER_LABELS.items()]


def apply_preset(preset_value: str, spec: Optional[Dict[str, str]] = None
                 ) -> Dict[str, str]:
    """套用预设：先把预设值铺满，再用用户已有的选择覆盖。"""
    p = PRESET_BY_VALUE.get(str(preset_value or "").strip())
    base = dict(p.spec) if p else {}
    base.update(normalize_spec(spec))
    return normalize_spec(base)


# 戴上正 zone 的选取方式：按层整体重掷，而不是把十五个维度全部推翻。
VARY_LAYERS = {"face": ("identity",), "full": ("identity", "styling")}


def vary_spec(base: Dict[str, str], mode: str = "face",
              seed: Optional[int] = None) -> Dict[str, str]:
    """在一份已有 spec 上"再生成一版"。

    为什么不用"让模型重写一遍"来实现：
    —— 那样改的往往是措辞，**换汤不换药**，用户点三遍还是同一张脸。

    真正的"换一版"是**换掉它的组成成分**。但完全随机同样糟糕：任何穿搭都能配
    任何光位，抽到"运动服 + 红唇 + 单硬光 4:1"这种组合的概率一点都不低，出来的
    图一定不能用。所以要**按层整体重掷**，而不是把十五个维度全盘推翻：

        face —— 只重掷身份层（脸型/肤色/身材/气质/神情），服装与画面保持不变。
                用户抱怨"想换张脸"时这是正确的粒度。
        full —— 身份层 + 造型层一起重掷，画面层保留（用户已经调好的布光不该被冲掉）。

    重掷保证**每一项都真的和上一版不同**（而不是让用户点了半天只换了个表情）。
    """
    import random

    rng = random.Random(seed if seed is not None else None)
    out = normalize_spec(base)
    layers = VARY_LAYERS.get(mode, VARY_LAYERS["face"])
    for layer in layers:
        for key in LAYER_KEYS.get(layer, ()):
            dim = DIM_BY_KEY[key]
            wanted = {o.value for o in dim.options if o.zhp or o.en}
            prev = out.get(key)
            # 只有两个以下候选时没得挑，随机就失去了意义
            if len(wanted) > 1:
                wanted.discard(prev)
            out[key] = rng.choice(sorted(wanted))
    return out
