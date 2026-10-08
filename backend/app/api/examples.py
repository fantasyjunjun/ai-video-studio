"""内置示例商品：一键把 3 条示例塞进商品库，让新用户能立刻试批量出片。

对齐 ClipForge 的「导入示例商品」。这些是**出厂示例**，与用户自建数据分开：

  - 数据在代码里（`EXAMPLE_PRODUCTS`），不写进任何库；
  - 前端点「导入示例商品」时逐条调 `POST /api/products`，走的还是正常入库路径，
    所以入的库就是普通商品，之后能改能删 —— **不做"系统商品"这种特殊标记**，
    特殊标记会让后面每条查询都要加过滤条件，得不偿失；
  - 图片是打包在 `data/uploads/examples/default/` 下的示例图（随包分发），
    URL 与手工上传的图同构，前端一视同仁地渲染。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..services import storage

router = APIRouter(prefix="/api", tags=["examples"])

# 与商品库的品类枚举一致（beauty/food/home/fashion/tech/other）
EXAMPLE_PRODUCTS = [
    {
        "name": "便携榨汁杯",
        "category": "tech",
        "description": (
            "USB 充电随身榨，30 秒一杯鲜榨果汁；六叶刀头碎冰碎果，"
            "办公室、健身房、出差都能用；杯体可水洗，清洗 0 负担。"
        ),
        "price": "129",
        "target_audience": "20-35 岁健身与通勤人群",
        "image": "juicer",
    },
    {
        "name": "冷萃咖啡液",
        "category": "food",
        "description": (
            "0 糖 0 脂，3 秒冲一杯；冷热都好喝，兑水兑奶皆可；"
            "独立小包装随身带，上班族续命、健身控糖都适合。"
        ),
        "price": "59",
        "target_audience": "22-40 岁上班族",
        "image": "coffee",
    },
    {
        "name": "云柔加厚抽纸",
        "category": "home",
        "description": (
            "加厚 3 层，湿水不破不掉屑；原生木浆亲肤不刺激，宝宝孕妇可用；"
            "整箱囤更划算，家用车用办公都合适。"
        ),
        "price": "39",
        "target_audience": "家庭主妇 / 母婴人群",
        "image": "tissue",
    },
]


@router.get("/examples/products")
def example_products():
    """示例商品清单。图片走上传域，URL 形态与用户自传的图一致。"""
    d = storage.scope_dir("examples", "default", mkdir=False)

    def image_url(basename: str) -> str:
        """按 png → jpg → webp → svg 的顺序取第一张存在的示例图。

        出厂带的是**本地合成的 svg**（零成本、随包分发，见
        `scripts/make_example_images.py`）。把所有常见格式都探一遍是为了
        "以后想把 svg 换成实拍照片"这件事不需要改代码 —— 丢个同名 png 进去即可。
        """
        for ext in (".png", ".jpg", ".webp", ".svg"):
            cand = f"{basename}{ext}"
            if (d / cand).is_file():
                return storage.url_for("examples", "default", cand)
        return storage.url_for("examples", "default", f"{basename}.svg")

    out = []
    for i, ex in enumerate(EXAMPLE_PRODUCTS, start=1):
        out.append({
            "id": f"ex-{i}",
            "name": ex["name"],
            "category": ex["category"],
            "description": ex["description"],
            "price": ex["price"],
            "target_audience": ex["target_audience"],
            "images": [image_url(ex["image"])],
        })
    return out
