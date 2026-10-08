/** 商品品类元数据（**前端唯一事实来源**）。
 *
 *  枚举本身以后端 `GET /api/products/categories` 为准（它同时负责校验），
 *  这里只补"怎么显示"：中文名与配色。配色按 ClipForge 的语义色走 ——
 *  不同品类一眼可辨是卡片网格能"扫"而不是"读"的前提。
 */

export type CategoryKey = 'beauty' | 'perfume' | 'food' | 'home' | 'fashion' | 'tech' | 'other';

export interface CategoryMeta {
  key: CategoryKey;
  label: string;
  /** 主色（暗底上用） */
  color: string;
  /** 半透明底 + 同色文字，做 badge */
  bg: string;
}

export const CATEGORIES: CategoryMeta[] = [
  { key: 'beauty', label: '美妆护肤', color: '#ff85c0', bg: 'rgba(255,133,192,0.16)' },
  { key: 'perfume', label: '香水香氛', color: '#ff9c6e', bg: 'rgba(255,156,110,0.16)' },
  { key: 'food', label: '食品零食', color: '#ffc53d', bg: 'rgba(255,197,61,0.16)' },
  { key: 'home', label: '家居日用', color: '#69b1ff', bg: 'rgba(105,177,255,0.16)' },
  { key: 'fashion', label: '服饰鞋包', color: '#b37feb', bg: 'rgba(179,127,235,0.16)' },
  { key: 'tech', label: '数码 3C', color: '#36cfc9', bg: 'rgba(54,207,201,0.16)' },
  { key: 'other', label: '其他', color: '#a6a6a6', bg: 'rgba(166,166,166,0.16)' },
];

const BY_KEY: Record<string, CategoryMeta> = Object.fromEntries(
  CATEGORIES.map((c) => [c.key, c]),
);

/** 未知/缺失品类一律落到"其他"，**绝不返回 undefined** ——
 *  卡片右上角少个 badge 无所谓，抛异常把整页打白不行。 */
export function categoryMeta(key?: string | null): CategoryMeta {
  return BY_KEY[(key || '').toLowerCase()] || BY_KEY.other;
}

export function categoryLabel(key?: string | null): string {
  return categoryMeta(key).label;
}

export const CATEGORY_OPTIONS = CATEGORIES.map((c) => ({ value: c.key, label: c.label }));
