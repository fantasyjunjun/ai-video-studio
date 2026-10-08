"""广告法合规自检（P-4）—— 纯函数词表扫描，零依赖、零密钥、可单测。

为什么需要它
------------
香水/美妆带货片是《广告法》的重灾区：一句"最持久"、"美白淡斑"
就可能招来行政罚款（虚假广告，广告费用 3–5 倍罚款起步）。
而这类违规**全部发生在文案层**，与出片质量无关 ——
所以它必须是一道**独立于生成的发布前闸门**，而不是靠人肉记忆。

设计原则（与 lint.py 同构，便于一起维护）
----------------------------------------
1. **纯函数**：`scan_text / scan_sources` 不碰网络、不碰 DB、不打日志。
2. **词表即规则**：规则是数据（`Rule`），不是散落在代码里的 if。
   用户可以拿 `compliance.yaml` 覆盖/追加，代码零改动
   —— 与"任意中转站可配"同一思路：**把差异留在配置里**。
3. **多语言但不是翻译**：中国主体投放巴西市场时，
   中文文案归《广告法》管、葡语文案归巴西 CONAR/CDC 管，
   **两套词表各自独立命中**（不做互译），因为两国禁语并不对应
   （例如巴西禁"100% garantido"，中国禁"国家级"）。
4. **三级严重度 + 精确 span**：high 才阻断发布；span 供前端标红。
5. **豁免必须显式**：中文"最后/最初"、"独一无二的你"、
   葡语"melhor amigo"都是正常修辞，**默认就该不报**。
   靠"词表写长 + GLOBAL_EXCEPTIONS"两层挡，而不是事后放宽阈值。

判定契约
--------
- `high`   → 明确违法（绝对化用语 / 医疗功效 / 疗效承诺）→ **阻断发布**
- `medium` → 高风险需举证（虚假紧迫 / 绝对保证 / 无依据比较 / 伪科学概念）
- `low`    → 边界模糊的功效描述与程度词堆砌 → 仅提示
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------- 常量

CATEGORY_LABELS: Dict[str, str] = {
    "absolute": "绝对化用语",
    "medical": "医疗/功效宣称",
    "urgency": "虚假紧迫",
    "guarantee": "绝对承诺",
    "compare": "无依据比较排名",
    "fake_science": "伪科学概念",
    "hedge": "边界模糊表述",
    "exaggeration": "夸大宣传",
}

LANG_LABELS: Dict[str, str] = {
    "zh": "中文（《广告法》）",
    "pt": "葡萄牙语（巴西 CONAR/CDC）",
    "es": "西班牙语（拉美消费者保护）",
}

SEVERITY_RANK: Dict[str, int] = {"high": 3, "medium": 2, "low": 1}
SEVERITY_LABELS: Dict[str, str] = {
    "high": "高危 · 阻断发布",
    "medium": "高风险 · 需举证",
    "low": "提示 · 建议改写",
}

# 回传原文的上限：超过就不回（前端退化为列表展示，不影响命中与 span）
MAX_ECHO_TEXT = 20000


@dataclass
class Rule:
    """一条合规规则 = 一个类别的若干禁语正则。"""

    id: str
    lang: str
    category: str
    severity: str
    patterns: List[str]
    note: str = ""
    suggestion: str = ""
    law: str = ""
    # 仅对**本条规则**生效的豁免（上下文窗口内命中即忽略）
    exceptions: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "lang": self.lang, "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, self.category),
            "severity": self.severity, "note": self.note,
            "suggestion": self.suggestion, "law": self.law,
            "patterns": list(self.patterns),
        }


# ---------------------------------------------------------------- 全局豁免
#
# 命中禁语但属于**正常修辞 / 序数用法**的语境，一律不算违规。
# 放在全局层（而非逐条规则）的原因：这些词在任何规则下都不该报，
# 且它们是"中文文学化文案"的常见写法（本项目五段式恰恰鼓励这种写法）。

GLOBAL_EXCEPTIONS: List[str] = [
    # ---- 中文：序数与时间副词
    r"最(后|初|终|近)",
    r"第[一二三四五六七八九十](?!品牌|选择|名|位|好|优选)",
    r"一(而再|如既往)",
    # ---- 中文：正常修辞
    r"唯一(的)?(你|自己|它|他|她|时刻|夜晚)",
    r"独一无二的?(你|自己|时刻|夜晚)",
    r"完美的?(不完美|遗憾|错过)",
    r"绝对的?(自由|安静|专注|沉静|顺从)",
    r"至(尊|臻)(的)?(客人|你)",
    # ---- 葡语：melhor/perfeito 的日常用法
    r"melhor\s+(amig|moment|cois|part|vers)",
    r"perfeit[ao]\s+(para\s+mim|noite|momento|dia|combina)",
    r"[úu]nic[ao]\s+(para\s+voc[êe]|em\s+voc[êe])",
    # ---- 西语
    r"mejor\s+(amig|moment|cos|part)",
    r"perfect[ao]\s+(para\s+m[ií]|noche|momento|d[ií]a)",
]


# ---------------------------------------------------------------- 词表
#
# 说明：中文侧只列**具体组合**，不列裸"最"、"第一" ——
# 裸词在中文里误报率极高（最后/最初/第一缕），且在词表层挡掉
# 比在豁免层挡更便宜、更可读。

ZH_ABSOLUTE = [
    r"最(好|佳|优|强|先进|有效|便宜|实惠|划算|超值|流行|受欢迎|畅销|权威|专业|领先|安全|健康|科学|高级|顶级|大|小|棒|完美|满意|知名|著名|重要|耐用|持久|火爆)",
    r"(全国|世界|全球|中国|亚洲|行业|销量|市场|口碑|排名|人气|全网|全行业)第一",
    r"第一(品牌|选择|名|优选|推荐)",
    r"(国家级|世界级|国际级|顶级|顶尖|极品|极佳|极致|之最|至尊)",
    r"(独一无二|绝无仅有|史无前例|前所未有|无与伦比|登峰造极|首屈一指|遥遥领先|独占鳌头|独步天下)",
    r"(领导|领先|标杆|龙头|霸主)(品牌|企业|地位|水平)",
    r"(完美无缺|绝对|百分百|100\s*%|无条件)",
    r"(纯天然|天然无添加|零添加|零刺激|零负担|零风险)",
]

ZH_MEDICAL = [
    r"(美白|淡斑|祛斑|去斑|祛痘|去痘|除痘|祛皱|去皱|除皱|抗皱|去黑眼圈|去眼袋)",
    r"(抗衰老|抗老化|逆龄|驻颜|冻龄|重返青春|返老还童|细胞再生|修复再生|促进再生)",
    r"(治疗|治愈|医疗|医治|疗效|药效|药用|医用|处方|临床验证|临床证明|医学证明|经临床)",
    r"(抗菌|杀菌|抑菌|消毒|消炎|止痛|止痒|抗过敏|防过敏|脱敏|抗炎)",
    r"(减肥|瘦身|燃脂|消脂|减脂|丰胸|丰臀|壮阳|增高|生发|防脱发|乌发|生眉)",
    r"(排毒|祛湿|排湿|调理身体|改善睡眠|缓解疲劳|增强免疫|提高免疫|抗疲劳)",
    r"(医生推荐|皮肤科医生推荐|专家推荐|三甲医院|药妆|械字号|医用级)",
]

ZH_URGENCY = [
    r"(限时|限购|最后一天|仅限今天|仅限今日|马上结束|即将结束|活动仅剩)",
    r"(仅剩|只剩|仅余|最后\s*\d+\s*(件|瓶|套|份|盒))",
    r"(抢购|秒杀|疯抢|抢完即止|售完即止|错过不再|手慢无|欲购从速)",
    r"(立即下单|马上购买|赶紧下单|赶快下单|不要错过|千万别错过|最后机会|名额有限|数量有限)",
]

ZH_GUARANTEE = [
    r"(保证|承诺|确保)(有效|见效|效果|疗效|满意|变白|变美|不反弹)",
    r"(无效退款|无效包退|不满意退款|用过不满意退)",
    r"(一次见效|立竿见影|立刻见效|马上见效|当天见效|立即见效|永久有效|终身有效)",
    r"(效果|结果|功效)(保证|承诺)",
    r"(永久不反弹|用不反弹|一劳永逸)",
]

ZH_COMPARE = [
    r"(效果|功效|表现)(远超|胜过|优于|好于|超过)",
    r"(远胜|远优于|遥遥领先于|全面超越|大幅超越)",
    r"(是|比).{0,10}(的)?\s*\d+\s*倍",
    r"(比|较)(同类|其他|普通|市面上|竞品).{0,8}(好|强|快|持久|优秀|出色|出色)",
    r"(超越|击败|吊打|碾压)(同类|竞品|大牌|一线品牌|国际大牌)",
]

ZH_FAKE_SCIENCE = [
    r"(量子|纳米|石墨烯|干细胞|基因|细胞级|分子级|DNA)\s*(技术|护肤|修复|抗衰|焕活|能量)",
    r"(黑科技|诺贝尔|实验室级|医美级|院线级|大牌同源|同厂同源|同源配方)",
]

ZH_HEDGE = [
    r"(提亮|亮肤|匀净|淡纹|抚纹|修护|舒缓|镇静|紧致|嫩肤|焕肤|活肤)",
    r"(非常|特别|超级|极其|极为|十分|相当|无比)",
    r"(留香\s*\d+\s*(小时|天|日)|持续\s*\d+\s*小时|(持|留)香(一整天|整天|24\s*小时))",
]

ZH_RULES: List[Rule] = [
    Rule(
        id="zh-absolute-superlative", lang="zh", category="absolute",
        severity="high",
        law="《广告法》第九条第（三）项",
        note="禁止使用『国家级』『最高级』『最佳』等绝对化用语",
        suggestion="改为可举证的具体表述：高浓度、精选、量肤定制、层次丰富",
        patterns=ZH_ABSOLUTE,
    ),
    Rule(
        id="zh-medical-efficacy", lang="zh", category="medical",
        severity="high",
        law="《广告法》第十七条、第二十八条",
        note="非医疗、药品广告不得涉及疾病治疗功能，不得使用医疗用语",
        suggestion="删去功效承诺，改写为感官体验：更贴肤、更清透、留香层次更清晰",
        patterns=ZH_MEDICAL,
    ),
    Rule(
        id="zh-urgency", lang="zh", category="urgency",
        severity="medium",
        law="《广告法》第二十八条（虚假或引人误解的内容）",
        note="制造虚假紧迫感、限时诱导属于误导性宣传",
        suggestion="去掉倒计时与催单话术，改用场景钩子收尾",
        patterns=ZH_URGENCY,
    ),
    Rule(
        id="zh-guarantee", lang="zh", category="guarantee",
        severity="medium",
        law="《广告法》第二十八条",
        note="效果保证、一次见效类承诺无法举证即为虚假广告",
        suggestion="改为描述性表达：用起来是这个感觉、每次喷都想起那个夜晚",
        patterns=ZH_GUARANTEE,
    ),
    Rule(
        id="zh-compare", lang="zh", category="compare",
        severity="medium",
        law="《广告法》第十三条、第二十八条",
        note="不得贬低同行，无依据的对比与倍数宣称属虚假宣传",
        suggestion="删去对比对象与倍数，只描述自身特点",
        patterns=ZH_COMPARE,
    ),
    Rule(
        id="zh-fake-science", lang="zh", category="fake_science",
        severity="medium",
        law="《广告法》第二十八条",
        note="伪科学概念与无法验证的技术宣称",
        suggestion="改用可感知的表述：雾感细腻、贴合皮肤的凉意",
        patterns=ZH_FAKE_SCIENCE,
    ),
    Rule(
        id="zh-hedge", lang="zh", category="hedge",
        severity="low",
        law="《化妆品监督管理条例》（功效宣称需依据）",
        note="边界模糊的功效词与程度词堆砌，发布前宜确认是否有宣称依据",
        suggestion="程度词未必违法但会稀释文案质感，建议删掉换成具体意象",
        patterns=ZH_HEDGE,
    ),
]

# ---------------------------------------------------------------- 葡语（巴西）
#
# 巴西不是"绝对化全禁"，而是**必须可举证**（CONAR 自律守则 + CDC 第 37 条
# 误导性广告）。因此把"疗效承诺"与"可举证宣称"分成两档：
# cura/elimina acne/emagrece 这类 → high；clareador/dermatologicamente testado
# 这类在有 ANVISA 依据时合法 → medium（提示补依据）。

PT_ABSOLUTE_HARD = [
    # "o melhor perfume do mundo" —— 中间常夹名词，所以允许 0–3 个词
    r"\b(o|a|os|as)\s+melhor(es)?\s+(\w+\s+){0,3}(do|da|de)\s+(mundo|mercado|pa[íi]s|brasil|categoria|segmento)\b",
    r"\bmelhor\s+(do|da)\s+(mundo|mercado|pa[íi]s|brasil)\b",
    r"\bn[úu]mero\s*1\b|\bn[º°]\s*1\b|\bnumber\s*one\b",
    r"\b(o|a)\s+[úu]nic[ao]\s+(no|do|da|em)\s+(mercado|mundo|brasil|segmento|categoria)\b",
    r"\bincompar[áa]vel\b|\bimbat[íi]vel\b|\binsuper[áa]vel\b|\bsem\s+igual\b|\bsem\s+comparação\b",
    r"\bl[íi]der\s+(absoluto|de\s+mercado|nacional|mundial|isolado)\b",
    r"\b100\s*%\s*(garantido|eficaz|natural|puro|seguro|comprovado)\b",
    r"\b(o|a)\s+mais\s+(eficaz|eficiente|poderos[ao]|vendid[ao]|desejad[ao]|conhecid[ao]|segur[ao]|avan[çc]ad[ao])\b",
]

PT_ABSOLUTE_SOFT = [
    r"\bperfeit[ao]\b",
    r"\bdefinitiv[ao]\b",
    r"\b(o|a)\s+melhor\s+(perfume|produto|marca|op[çc][ãa]o|escolha)\b",
    r"à\s+prova\s+de\s+(água|erros?|falhas?|tempo)",
]

PT_MEDICAL_HARD = [
    r"\b(cura|cura\s+definitiva|tratamento\s+(de|para|capilar|facial)|medicinal|terap[êe]utic[ao])\b",
    r"\b(elimina|acaba\s+com|combate|remove|trata)\s+(a\s+|as\s+|o\s+|os\s+)?(acne|espinha|celulite|rugas|flacidez|gordura|manchas|queda\s+de\s+cabelo)\b",
    r"\b(emagrece|emagrecimento|queima\s+gordura|desintoxica|detox|drena|reduz\s+celulite)\b",
    r"\b(rejuvenesce|regenera|regenerador|antienvelhecimento|anti-?idade|antirrugas|anti-?acne)\b",
    r"\b(desinflama|anti-?inflamat[óo]ri[ao]|antiss[ée]ptic[ao]|antibacterian[ao]|antif[úu]ngic[ao]|cicatrizante)\b",
]

PT_MEDICAL_SOFT = [
    r"\b(clareia|clareador[ao]?|clareamento|despigmenta|remove\s+manchas|tira\s+manchas|anti-?manchas)\b",
    r"\b(dermatologicamente\s+testad[ao]|aprovad[ao]\s+por\s+dermatologistas?|recomendad[ao]\s+por\s+m[ée]dic[ao]s?|testad[ao]\s+clinicamente)\b",
    r"\b(hidrata[çc][ãa]o\s+profunda|nutri[çc][ãa]o\s+intensa)\b",
]

PT_URGENCY = [
    r"\bs[óo]\s+(hoje|agora|por\s+hoje|nessa\s+semana)\b",
    r"\b[úu]ltim[ao]s?\s+(unidades|chance|oportunidade|dias|pe[çc]as)\b",
    r"\b(n[ãa]o\s+perca|corra|aproveite\s+agora|compre\s+agora|clique\s+j[áa]|garanta\s+o\s+seu|pe[çc]a\s+j[áa])\b",
    r"\b(vagas?\s+limitad[ao]s?|estoque\s+limitado|promo[çc][ãa]o\s+rel[âa]mpago|por\s+tempo\s+limitado)\b",
]

PT_GUARANTEE = [
    r"\b(garantido|garantia\s+de\s+resultado|resultado\s+garantido|100\s*%\s*garantido)\b",
    r"\b(efeito|resultado)\s+imediato\b|\bresultado\s+em\s+\d+\s+(dias|semanas|meses)\b",
    r"\b(dinheiro\s+de\s+volta|se\s+n[ãa]o\s+(funcionar|gostar).{0,20}devolvemos)\b",
]

PT_FAKE_SCIENCE = [
    r"\b(qu[âa]ntic[ao]|nanotecnologia|nanotech|c[ée]lulas?\s+tronco|basead[ao]\s+em\s+dna)\b",
    r"\b(black\s+tech|nobel|n[íi]vel\s+de\s+laborat[óo]rio|n[íi]vel\s+cl[íi]nic[ao])\b",
]

PT_HEDGE = [
    r"\b(ilumina|uniformiza|suaviza|firma|revitaliza|renova)\s+(a\s+)?(pele|a\s+pele)?\b",
    r"\b(muito|extremamente|super|ultra)\s+(eficaz|eficiente|poderos[ao]|concentrad[ao])\b",
    r"\b(fixa[çc][ãa]o\s+de\s+\d+\s+horas|dura\s+\d+\s+horas|fixa\s+por\s+\d+\s+horas)\b",
]

PT_RULES: List[Rule] = [
    Rule(
        id="pt-absolute-hard", lang="pt", category="absolute", severity="high",
        law="CONAR 自律守则 §3；CDC 第 37 条（误导性广告）",
        note="『melhor do mundo / nº 1 / único / imbatível』属绝对化宣称，须可举证",
        suggestion="改为可支撑的表述：notas marcantes、presença longa、assinatura própria",
        patterns=PT_ABSOLUTE_HARD,
    ),
    Rule(
        id="pt-absolute-soft", lang="pt", category="absolute", severity="medium",
        law="CONAR 自律守则 §3",
        note="perfeito/definitivo 在葡语里常作修辞，但广告语境仍属绝对化，宜改",
        suggestion="改用 notável、marcante、inesquecível 等非绝对表述",
        patterns=PT_ABSOLUTE_SOFT,
    ),
    Rule(
        id="pt-medical-hard", lang="pt", category="medical", severity="high",
        law="ANVISA RDC 752/2022；CDC 第 37 条",
        note="化妆品不得宣称治疗、治愈或消除病症",
        suggestion="删去疗效承诺，改写为使用感受：na pele, é leve e macio",
        patterns=PT_MEDICAL_HARD,
    ),
    Rule(
        id="pt-medical-soft", lang="pt", category="medical", severity="medium",
        law="ANVISA RDC 752/2022（功效宣称须有依据）",
        note="clareador / dermatologicamente testado 等宣称需有注册或测试依据",
        suggestion="确认依据后再保留，或改为以感官为主的描述",
        patterns=PT_MEDICAL_SOFT,
    ),
    Rule(
        id="pt-urgency", lang="pt", category="urgency", severity="medium",
        law="CDC 第 37 条",
        note="虚假紧迫（só hoje / últimas unidades）属误导",
        suggestion="收尾改用主权宣言：a noite inteira é sua",
        patterns=PT_URGENCY,
    ),
    Rule(
        id="pt-guarantee", lang="pt", category="guarantee", severity="medium",
        law="CDC 第 37 条",
        note="结果保证与即时见效类承诺难以举证",
        suggestion="改为描述性表达：quem sente, lembra",
        patterns=PT_GUARANTEE,
    ),
    Rule(
        id="pt-fake-science", lang="pt", category="fake_science", severity="medium",
        law="CONAR 自律守则 §3",
        note="无法验证的技术概念（quântico / nanotecnologia）",
        suggestion="改用可感知的表述：a névoa fina que assenta na pele",
        patterns=PT_FAKE_SCIENCE,
    ),
    Rule(
        id="pt-hedge", lang="pt", category="hedge", severity="low",
        law="CONAR 自律守则（可举证性）",
        note="边界功效词与固定时长宣称，发布前确认依据",
        suggestion="程度词会稀释质感，建议换成具体意象",
        patterns=PT_HEDGE,
    ),
]

# ---------------------------------------------------------------- 西语（拉美）

ES_ABSOLUTE_HARD = [
    r"\b(el|la|los|las)\s+mejor(es)?\s+(\w+\s+){0,3}(del|de)\s+(mundo|mercado|pa[íi]s|categor[íi]a)\b",
    r"\bmejor\s+del\s+(mundo|mercado|pa[íi]s)\b",
    r"\bn[úu]mero\s*1\b|\bn[º°]\s*1\b",
    r"\b(el|la)\s+[úu]nic[ao]\s+(en|del|de)\s+(el\s+)?(mercado|mundo|categor[íi]a)\b",
    r"\bincomparable\b|\binsuperable\b|\bsin\s+igual\b|\bsin\s+comparaci[óo]n\b",
    r"\bl[íi]der\s+(absoluto|del\s+mercado|nacional|mundial)\b",
    r"\b100\s*%\s*(garantizado|eficaz|natural|puro|seguro)\b",
    r"\b(el|la)\s+m[áa]s\s+(eficaz|eficiente|poderos[ao]|vendid[ao]|desead[ao])\b",
]

ES_ABSOLUTE_SOFT = [
    r"\bperfect[ao]\b",
    r"\bdefinitiv[ao]\b",
    r"\b(el|la)\s+mejor\s+(perfume|producto|marca|opci[óo]n|elecci[óo]n)\b",
]

ES_MEDICAL_HARD = [
    r"\b(cura|tratamiento\s+(de|para)|medicinal|terap[ée]utic[ao])\b",
    r"\b(elimina|acaba\s+con|combate|quita|remueve|trata)\s+(el\s+|la\s+|los\s+|las\s+)?(acn[ée]|barros|celulitis|arrugas|flacidez|grasa|manchas)\b",
    r"\b(adelgaza|quema\s+grasa|desintoxica|detox|reduce\s+celulitis)\b",
    r"\b(rejuvenece|regenera|antiedad|antiarrugas|anti-?acn[ée])\b",
]

ES_MEDICAL_SOFT = [
    r"\b(aclara|aclarador[ao]?|aclaramiento|blanquea|blanqueador[ao]?|quita\s+manchas|anti-?manchas)\b",
    r"\b(dermatol[óo]gicamente\s+probado|aprobado\s+por\s+dermat[óo]logos?|recomendado\s+por\s+m[ée]dicos?)\b",
]

ES_URGENCY = [
    r"\bsolo\s+(hoy|ahora|por\s+hoy)\b",
    r"\b[úu]ltim[ao]s?\s+(unidades|oportunidad|d[íi]as|piezas)\b",
    r"\b(no\s+te\s+lo\s+pierdas|corre|aprovecha\s+ahora|compra\s+ahora|pide\s+ya)\b",
    r"\b(cupos?\s+limitad[ao]s?|stock\s+limitado|promoci[óo]n\s+rel[áa]mpago)\b",
]

ES_GUARANTEE = [
    r"\b(garantizado|garant[íi]a\s+de\s+resultado|resultado\s+garantizado)\b",
    r"\b(efecto|resultado)\s+inmediato\b|\bresultado\s+en\s+\d+\s+(d[íi]as|semanas)\b",
    r"\b(devolvemos\s+tu\s+dinero|si\s+no\s+(funciona|te\s+gusta).{0,20}devolvemos)\b",
]

ES_FAKE_SCIENCE = [
    r"\b(cu[áa]ntic[ao]|nanotecnolog[íi]a|nanotech|c[ée]lulas?\s+madre)\b",
    r"\b(black\s+tech|nobel|nivel\s+de\s+laboratorio|nivel\s+cl[íi]nic[ao])\b",
]

ES_HEDGE = [
    r"\b(ilumina|unifica|suaviza|firma|revitaliza|renueva)\b",
    r"\b(muy|extremadamente|s[úu]per)\s+(eficaz|eficiente|poderos[ao]|concentrad[ao])\b",
    r"\b(fijaci[óo]n\s+de\s+\d+\s+horas|dura\s+\d+\s+horas)\b",
]

ES_RULES: List[Rule] = [
    Rule(
        id="es-absolute-hard", lang="es", category="absolute", severity="high",
        law="墨西哥 LFPC 第 32 条 / 拉美各国误导广告规制",
        note="『el mejor del mundo / número 1 / inigualable』属绝对化宣称",
        suggestion="改为可支撑的表述：notas que se recuerdan、presencia que dura",
        patterns=ES_ABSOLUTE_HARD,
    ),
    Rule(
        id="es-absolute-soft", lang="es", category="absolute", severity="medium",
        law="LFPC 第 32 条",
        note="perfecto/definitivo 属绝对化修辞",
        suggestion="改用 memorable、distintivo 等非绝对表述",
        patterns=ES_ABSOLUTE_SOFT,
    ),
    Rule(
        id="es-medical-hard", lang="es", category="medical", severity="high",
        law="COFEPRIS / LFPC（化妆品不得宣称疗效）",
        note="化妆品不得宣称治疗功效",
        suggestion="删去疗效承诺，改写为质感描述",
        patterns=ES_MEDICAL_HARD,
    ),
    Rule(
        id="es-medical-soft", lang="es", category="medical", severity="medium",
        law="COFEPRIS（功效宣称须有依据）",
        note="aclarador / dermatológicamente probado 需有依据",
        suggestion="确认依据后再保留",
        patterns=ES_MEDICAL_SOFT,
    ),
    Rule(
        id="es-urgency", lang="es", category="urgency", severity="medium",
        law="LFPC 第 32 条",
        note="虚假紧迫话术属误导",
        suggestion="收尾改用主权宣言式表达",
        patterns=ES_URGENCY,
    ),
    Rule(
        id="es-guarantee", lang="es", category="guarantee", severity="medium",
        law="LFPC 第 32 条",
        note="结果保证类承诺难以举证",
        suggestion="改为描述性表达",
        patterns=ES_GUARANTEE,
    ),
    Rule(
        id="es-fake-science", lang="es", category="fake_science", severity="medium",
        law="LFPC 第 32 条",
        note="无法验证的技术概念",
        suggestion="改用可感知的描述",
        patterns=ES_FAKE_SCIENCE,
    ),
    Rule(
        id="es-hedge", lang="es", category="hedge", severity="low",
        law="可举证性要求",
        note="边界功效词与时长宣称",
        suggestion="换成具体意象",
        patterns=ES_HEDGE,
    ),
]

DEFAULT_RULES: List[Rule] = ZH_RULES + PT_RULES + ES_RULES


# ---------------------------------------------------------------- 编译

_compiled_cache: Dict[str, "_Compiled"] = {}


class _Compiled:
    __slots__ = ("rule", "regexes", "excepts")

    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self.regexes = [re.compile(p, re.IGNORECASE) for p in rule.patterns]
        self.excepts = [re.compile(p, re.IGNORECASE) for p in rule.exceptions]


def compile_rules(rules: Sequence[Rule]) -> List[_Compiled]:
    """编译为可复用对象；同一条规则只编译一次（按 id+pattern 指纹缓存）。

    每次请求都重新 compile 正则会让"前端边打字边扫描"变卡，
    所以这里做进程级缓存；规则内容变了指纹就变，不会读到旧正则。
    """
    out: List[_Compiled] = []
    for r in rules:
        key = r.id + "|" + "|".join(r.patterns) + "#" + "|".join(r.exceptions)
        hit = _compiled_cache.get(key)
        if hit is None:
            hit = _Compiled(r)
            _compiled_cache[key] = hit
        out.append(hit)
    return out


_GLOBAL_EXC = [re.compile(p, re.IGNORECASE) for p in GLOBAL_EXCEPTIONS]


# ---------------------------------------------------------------- 结果结构


@dataclass
class Finding:
    """一次命中。`start`/`end` 是相对该段文本的下标，供前端精确标红。"""

    rule_id: str
    lang: str
    category: str
    severity: str
    matched: str
    start: int
    end: int
    context: str
    note: str = ""
    suggestion: str = ""
    law: str = ""
    source: str = ""

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id, "lang": self.lang,
            "category": self.category, "category_label": self.category_label,
            "severity": self.severity, "severity_label": SEVERITY_LABELS.get(self.severity, self.severity),
            "matched": self.matched, "start": self.start, "end": self.end,
            "context": self.context, "note": self.note,
            "suggestion": self.suggestion, "law": self.law, "source": self.source,
        }


@dataclass
class ScanResult:
    """一段文本的扫描结果。

    `text` 回传原文是**前端标红所必需**（只有 span 没有原文，用户看到的
    是一串数字）。合规文案通常几十到几百字，回传成本可忽略；
    超过 `MAX_ECHO_TEXT` 的超长文本则不回原文，只给命中和 span ——
    宁可让前端退化成"列表展示"，也不要把请求体撑爆。
    """

    source: str
    lang_hint: str = ""
    text_len: int = 0
    text: str = ""
    findings: List[Finding] = field(default_factory=list)

    @property
    def counts(self) -> Dict[str, int]:
        c = {"high": 0, "medium": 0, "low": 0}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    @property
    def blocked(self) -> bool:
        return self.counts["high"] > 0

    @property
    def worst(self) -> str:
        for s in ("high", "medium", "low"):
            if self.counts[s]:
                return s
        return "clean"

    def to_dict(self) -> dict:
        return {
            "source": self.source, "lang_hint": self.lang_hint,
            "text_len": self.text_len, "text": self.text,
            "counts": self.counts,
            "total": len(self.findings), "blocked": self.blocked,
            "worst": self.worst, "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class ComplianceReport:
    """整个项目（或一次交付）的合规报告。"""

    items: List[ScanResult] = field(default_factory=list)
    langs: List[str] = field(default_factory=list)
    rule_count: int = 0
    rules_source: str = "builtin"

    @property
    def findings(self) -> List[Finding]:
        out: List[Finding] = []
        for it in self.items:
            out.extend(it.findings)
        return out

    @property
    def counts(self) -> Dict[str, int]:
        c = {"high": 0, "medium": 0, "low": 0}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    @property
    def blocked(self) -> bool:
        return self.counts["high"] > 0

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "blocked": self.blocked, "counts": self.counts,
            "total": len(self.findings), "langs": self.langs,
            "rule_count": self.rule_count, "rules_source": self.rules_source,
            "items": [i.to_dict() for i in self.items],
        }

    def summary_text(self) -> str:
        """给 HTTP detail / 日志用的一句话摘要（不含原文，避免日志污染）。"""
        c = self.counts
        if not any(c.values()):
            return "合规自检通过：未命中任何禁语"
        return (f"合规自检发现问题：高危 {c['high']} / 高风险 {c['medium']} / 提示 {c['low']}"
                + ("（含高危项，已阻断发布）" if self.blocked else ""))


# ---------------------------------------------------------------- 扫描


def _window(text: str, start: int, end: int, pad: int = 12) -> str:
    return text[max(0, start - pad):min(len(text), end + pad)]


def _exempted(window: str, rule_excepts: Sequence[re.Pattern]) -> bool:
    for rx in _GLOBAL_EXC:
        if rx.search(window):
            return True
    for rx in rule_excepts:
        if rx.search(window):
            return True
    return False


def scan_text(
    text: str,
    *,
    source: str = "",
    langs: Optional[Iterable[str]] = None,
    rules: Optional[Sequence[Rule]] = None,
    lang_hint: str = "",
) -> ScanResult:
    """扫描一段文本，返回带精确 span 的命中列表。

    langs=None 表示扫描所有语言的词表 —— 这是默认行为，因为一段文案里
    中葡混排很常见（品牌名保留原样、中文备注夹在中间），
    按语言"只扫一种"反而会漏。
    """
    res = ScanResult(source=source, lang_hint=lang_hint, text_len=len(text or ""))
    if not text:
        return res
    if len(text) <= MAX_ECHO_TEXT:
        res.text = text

    wanted = set(langs) if langs else None
    compiled = compile_rules(list(rules) if rules is not None else DEFAULT_RULES)

    seen: set = set()
    for c in compiled:
        if wanted is not None and c.rule.lang not in wanted:
            continue
        for rx in c.regexes:
            for m in rx.finditer(text):
                start, end = m.start(), m.end()
                key = (start, end, c.rule.id)
                if key in seen:
                    continue
                if _exempted(_window(text, start, end), c.excepts):
                    continue
                seen.add(key)
                res.findings.append(Finding(
                    rule_id=c.rule.id, lang=c.rule.lang,
                    category=c.rule.category, severity=c.rule.severity,
                    matched=m.group(0), start=start, end=end,
                    context=_window(text, start, end, pad=16),
                    note=c.rule.note, suggestion=c.rule.suggestion,
                    law=c.rule.law, source=source,
                ))

    res.findings.sort(key=lambda f: (f.start, -SEVERITY_RANK.get(f.severity, 0)))
    res.findings = _dedupe_overlaps(res.findings)
    res.findings = _suppress_within_category(res.findings)
    return res


def _suppress_within_category(findings: List[Finding]) -> List[Finding]:
    """同语言同类别里，被高严重度命中覆盖的低严重度命中不再单列。

    实例：葡语 "O melhor perfume do mundo" 同时命中
    `pt-absolute-hard`（high，判定为绝对化世界第一）与
    `pt-absolute-soft`（medium，只是'melhor'修辞提示）——
    两个结论指的是同一段文字，同时列出会让报告显得自相矛盾。
    只保留更重的那条；若该位置没被 hard 命中，soft 照常报。
    """
    kept: List[Finding] = []
    for f in findings:
        covered = any(
            g is not f and g.lang == f.lang and g.category == f.category
            and SEVERITY_RANK.get(g.severity, 0) > SEVERITY_RANK.get(f.severity, 0)
            and not (f.end <= g.start or f.start >= g.end)
            for g in findings
        )
        if not covered:
            kept.append(f)
    return kept


def _dedupe_overlaps(findings: List[Finding]) -> List[Finding]:
    """同一规则的**部分重叠**命中只留一条。

    实例："行业第一品牌" 会同时命中 `行业第一`（@0-4）与 `第一品牌`（@2-6）——
    两条 span 交叠，前端标红会互相覆盖，看起来像渲染 bug。
    这里按"起始靠前、跨度更长、severity 更高"优先，同规则内丢掉被包含的那条；
    **跨规则不去重**（同一片段被两类规则命中是不同视角，信息量更大）。
    """
    ordered = sorted(findings, key=lambda f: (f.start, -f.end, -SEVERITY_RANK.get(f.severity, 0)))
    kept: List[Finding] = []
    for f in ordered:
        if any(g.rule_id == f.rule_id and not (f.end <= g.start or f.start >= g.end)
               for g in kept):
            continue
        kept.append(f)
    kept.sort(key=lambda f: (f.start, -SEVERITY_RANK.get(f.severity, 0)))
    return kept


def scan_sources(
    sources: Sequence[tuple],
    *,
    langs: Optional[Iterable[str]] = None,
    rules: Optional[Sequence[Rule]] = None,
    rules_source: str = "builtin",
) -> ComplianceReport:
    """扫描多个文本源。`sources` 是 [(source_name, text), ...]，空文本自动跳过。"""
    used_rules = list(rules) if rules is not None else DEFAULT_RULES
    # 物化成 list：下面每次 scan_text 都会重新 set(langs)，
    # 传 generator 的话第二次就空了（语言过滤会静默失效）。
    lang_list: Optional[List[str]] = None if langs is None else [str(x) for x in langs]
    rep = ComplianceReport(
        langs=sorted({r.lang for r in used_rules}) if lang_list is None else sorted(set(lang_list)),
        rule_count=len(used_rules), rules_source=rules_source,
    )
    for name, text in sources:
        if not text or not str(text).strip():
            continue
        rep.items.append(scan_text(str(text), source=name, langs=lang_list, rules=used_rules))
    return rep


# ---------------------------------------------------------------- 外部词表

_RULE_KEYS = {"id", "lang", "category", "severity", "patterns",
              "note", "suggestion", "law", "exceptions"}


def _rule_from_dict(d: Dict[str, Any]) -> Rule:
    miss = [k for k in ("id", "lang", "category", "severity", "patterns") if k not in d]
    if miss:
        raise ValueError(f"compliance.yaml 规则缺少字段 {miss}: {d}")
    sev = str(d["severity"]).lower()
    if sev not in SEVERITY_RANK:
        raise ValueError(f"未支持的 severity：{sev}（应为 high/medium/low）")
    lang = str(d["lang"]).lower()
    if lang not in LANG_LABELS:
        raise ValueError(f"未支持的 lang：{lang}（应为 zh/pt/es）")
    pats = d["patterns"]
    if isinstance(pats, str):
        pats = [pats]
    if not pats:
        raise ValueError(f"规则 {d['id']} 的 patterns 为空")
    # 正则合法性早失败：非法正则在这里就报，别等扫描时才炸
    for p in pats:
        re.compile(p)
    exc = d.get("exceptions") or []
    if isinstance(exc, str):
        exc = [exc]
    for p in exc:
        re.compile(p)
    return Rule(
        id=str(d["id"]), lang=lang, category=str(d["category"]),
        severity=sev, patterns=[str(p) for p in pats],
        note=str(d.get("note") or ""), suggestion=str(d.get("suggestion") or ""),
        law=str(d.get("law") or ""), exceptions=[str(p) for p in exc],
    )


def load_rules(path: Optional[str | Path] = None) -> tuple:
    """加载规则：(rules, source_desc)。

    - 文件不存在 → 用内置词表，source = "builtin"
    - 文件存在 → 内置词表 **减去 disabled** **加上 rules**（追加，不覆盖），
      这样用户既停用得掉误报规则，也能补自己的行业禁语；
      source = "builtin+<文件名>"。

    用 yaml 而不是 json：与 providers.yaml 一致，且能写注释 ——
    合规词表**必须**能写注释说明"为什么禁这个词"，否则半年后没人敢改。
    """
    default_src = "builtin"
    if path is None:
        return list(DEFAULT_RULES), default_src
    p = Path(path)
    if not p.is_file():
        return list(DEFAULT_RULES), default_src

    import yaml

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    disabled = set(str(x) for x in (data.get("disabled") or []))
    extra_raw = data.get("rules") or []
    if not isinstance(extra_raw, list):
        raise ValueError("compliance.yaml 的 rules 应为列表")

    rules = [r for r in DEFAULT_RULES if r.id not in disabled]
    have = {r.id for r in rules}
    for d in extra_raw:
        if not isinstance(d, dict):
            raise ValueError(f"compliance.yaml 规则项应为字典：{d!r}")
        r = _rule_from_dict(d)
        if r.id in have:  # 同 id 视为**覆盖**内置规则
            rules = [x for x in rules if x.id != r.id]
        have.add(r.id)
        rules.append(r)
    return rules, f"builtin+{p.name}"


def rules_summary(rules: Sequence[Rule]) -> dict:
    by_cat: Dict[str, int] = {}
    by_lang: Dict[str, int] = {}
    by_sev: Dict[str, int] = {}
    for r in rules:
        by_cat[r.category] = by_cat.get(r.category, 0) + 1
        by_lang[r.lang] = by_lang.get(r.lang, 0) + 1
        by_sev[r.severity] = by_sev.get(r.severity, 0) + 1
    return {
        "count": len(rules),
        "by_category": by_cat, "by_lang": by_lang, "by_severity": by_sev,
        "categories": CATEGORY_LABELS, "langs": LANG_LABELS,
        "severities": SEVERITY_LABELS,
    }


# ---------------------------------------------------------------- 便捷入口


def check_copy(
    text: str, *, source: str = "copy", langs: Optional[Iterable[str]] = None,
    rules: Optional[Sequence[Rule]] = None,
) -> ScanResult:
    """文案自检的最短入口（供 /final、/verify 内部调用）。"""
    return scan_text(text, source=source, langs=langs, rules=rules)


def is_blocked(
    sources: Sequence[tuple], *, rules: Optional[Sequence[Rule]] = None,
) -> bool:
    """只问"能不能发"，不要详情（供门禁快速判断）。"""
    return scan_sources(sources, rules=rules).blocked
