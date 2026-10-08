import { useCallback, useEffect, useRef, useState } from 'react';
import {
  App as AntApp, Button, Card, Collapse, Input, InputNumber, Modal, Select, Space,
  Steps, Tag, Tooltip, Typography,
} from 'antd';
import {
  DeleteOutlined, EditOutlined, InboxOutlined, LoadingOutlined, PlusOutlined,
  ThunderboltOutlined, UploadOutlined, UserOutlined,
} from '@ant-design/icons';
import { api, type Json } from '../api';

/** 主播库：基本信息 → 形象设定 → 形象提示词 → 参考图。
 *
 *  几步是**按成本递增**排的，这也是这套交互的全部理由：
 *
 *    1. 基本信息（年龄/性别/国籍必填）—— 纯录入，零成本；
 *    2. 形象设定 —— 选人设预设或逐项点选，仍然零成本；
 *    3. 形象提示词 —— 调**文本大模型**润色，几乎不花钱，**可以反复试**；
 *    4. 参考图 —— 调**文生图**，真花钱，按下就计费。
 *
 *  所以第 3 步刻意做成"生成 → 不满意可手改 / 换一版 → 满意了再出图"：
 *  让人在便宜的那一步把脸定下来，而不是靠反复重出图去碰运气。
 *
 *  ## 为什么第 2 步存在
 *
 *  过去只让用户填"27 岁 / 女 / 中国"，剩下的交给模型自由发挥。看起来省事，实际是
 *  把最影响观感的变量（穿什么、在哪拍、怎么打光）全部外包给了随机性 —— 用户说
 *  "出的图不对"，却没有任何可以调整的地方。现在这十六个维度全部显式化：
 *
 *      身份层  脸型 / 肤色 / 身材 / 气质 / 神情     —— 这个人是谁
 *      造型层  服装 / 妆容 / 发型 / 发色 / 配饰     —— 这一身穿什么
 *      画面层  场景 / 光线 / 姿势 / 构图 / 色调 / 风格 —— 这一次怎么拍
 *
 *  **画面层只对"氛围图"生效**。正面图 / 三视图 / 定妆照是给下游当参考图用的，一旦
 *  写上"咖啡厅、逆光"，那张背景会被后续每一个镜头原样复刻进成片。这条限制由后端
 *  `reference_safe` 强制，界面上也明示了。
 *
 *  已有模特图的走"直接导入"，与生成链路殊途同归 —— 产物都登记成主播的
 *  参考图，留在库里复用。
 */
/** 图类型 → 中文标签的**兜底**。唯一真相是后端 `/api/talents/options` 的
 *  `image_kinds`（它从 `REGISTERABLE_TALENT_IMAGE_KINDS` 转出），这里只在
 *  options 还没加载回来的那一帧兜一下。 */
const KIND_LABELS_FALLBACK: Record<string, string> = {
  front: '正面图',
  threeview: '人物三视图',
  sheet: '2x2 四视图定妆照',
  poster: '氛围图',
  variant: '其他参考图',
};

/** 画廊里的分组顺序：**参考图在前，成品图在后**。
 *  前四种是下游要用的零件，氛围图是独立成品 —— 顺序本身就在表达这个关系。 */
const KIND_ORDER = ['front', 'threeview', 'sheet', 'variant', 'poster'];

/** 把一位主播的图按类型分组（只保留真的有图的类型，未知类型排在最后）。
 *
 *  **一种类型可能有多行**：重出一次正面图就多一行，`_talent_out` 的 `pick()`
 *  只取第一行当卡片封面 —— 这正是"生成了却看不见"的另一半原因，所以画廊
 *  必须把同类型的多张全部铺出来，而不是只显示一行。 */
function groupImages(t: Json): { kind: string; items: Json[] }[] {
  const all: Json[] = t.images || [];
  const present = Array.from(new Set(all.map((i) => i.kind)));
  const ordered = [
    ...KIND_ORDER.filter((k) => present.includes(k)),
    ...present.filter((k) => !KIND_ORDER.includes(k)),
  ];
  return ordered.map((kind) => ({ kind, items: all.filter((i) => i.kind === kind) }));
}

/** 这位主播一共有几张图。**卡片上的 "+N" 角标靠它** —— 只有一个缩略图时，
 *  用户根本不知道背后还存着三视图和氛围图。 */
const imgCount = (t: Json) => (t.images || []).length;

export default function PresentersPage() {
  const { message, modal } = AntApp.useApp();
  const [talents, setTalents] = useState<Json[]>([]);
  const [loading, setLoading] = useState(true);
  const [options, setOptions] = useState<Json>({
    genders: [{ value: 'female', label: '女' }, { value: 'male', label: '男' },
              { value: 'neutral', label: '中性' }],
    nationalities: ['中国', '日本', '韩国', '东南亚', '欧美', '拉美', '混血'],
  });
  const [notice, setNotice] = useState<string | null>(null);

  /** 形象图画廊（四类主播图都在这里看）。`focus` 只影响打开时提示哪一种。 */
  const [gallery, setGallery] = useState<{ t: Json; focus?: string } | null>(null);
  /** 大图查看：一次拿同类型的整组，看的时候可以直接翻到下一张。 */
  const [zoom, setZoom] = useState<{ imgs: Json[]; idx: number; name: string } | null>(null);

  // ---------------- 新建/改提示词 向导 ----------------
  /** mode=create 走三步（含建库与出图）；mode=appearance 只改已有主播的提示词。 */
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardMode, setWizardMode] = useState<'create' | 'appearance'>('create');
  const [wizardTid, setWizardTid] = useState<number | null>(null);
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<Json>({
    name: '', age: 27, gender: 'female', nationality: '中国',
    description: '', voice_style: '', appearance: '', hint: '', lang: 'en',
    preset: '', spec: {},
  });
  /** 模型最近一次生成的原文。用来判断"用户有没有手改过"——决定 appearance_source。 */
  const [genResult, setGenResult] = useState('');
  const [genning, setGenning] = useState(false);
  const [busy, setBusy] = useState(false);

  // ---------------- 出图 / 导入 ----------------
  /** 正在出图的 (主播id:图类型)。**按人+类型分别记**，给 A 出正面图不该锁住 B。 */
  const [rendering, setRendering] = useState<string[]>([]);
  const [importFor, setImportFor] = useState<Json | null>(null);
  const [importKind, setImportKind] = useState('front');
  const [importFiles, setImportFiles] = useState<File[]>([]);
  const [importing, setImporting] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [ts, opts] = await Promise.all([
        api.listTalents(),
        api.talentOptions().catch(() => null),
      ]);
      setTalents(ts);
      if (opts) setOptions(opts);
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => { void load(); }, [load]);

  // ---------------- 图的标签与画廊 ----------------

  /** 图类型的中文标签：**先问后端 options**，读不到才用兜底表。 */
  const kindLabel = useCallback((k: string) => {
    const hit = (options.image_kinds || []).find((x: Json) => x.value === k);
    return hit?.label || KIND_LABELS_FALLBACK[k] || k;
  }, [options.image_kinds]);

  /** 这一类是不是"成品图"（采用画面层）。同样来自后端 `allows_render`，
   *  前端不自己判断 —— 判错了会把"带场景的成品图"当成参考图解释给用户。 */
  const isRenderKind = useCallback((k: string) => {
    const hit = (options.image_kinds || []).find((x: Json) => x.value === k);
    return hit ? !!hit.allows_render : k === 'poster';
  }, [options.image_kinds]);

  /** 打开画廊。不传 focus 就是"看全部"。 */
  const openGallery = (t: Json, focus?: string) => setGallery({ t, focus });

  /** 移除一条图登记。**只删记录，磁盘文件保留** —— 与"删主播"的说法一致。 */
  const removeImage = (im: Json) => {
    modal.confirm({
      title: '移除这张图？',
      content: `#${im.id}（${kindLabel(im.kind)}）。只删这条登记记录，磁盘上的文件保留。`,
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const updated = await api.deleteTalentImage(im.id);
          message.success('已移除');
          // 画廊持有的是列表的快照，删完必须用响应体里的最新主播覆盖它，
          // 否则弹窗里那张被删的图还在（列表已经刷新了，两边会打架）。
          if (updated && updated.id) {
            setGallery((g) => (g ? { ...g, t: updated } : g));
          }
          setZoom(null);
          void load();
        } catch (e) {
          message.error((e as Error).message);
        }
      },
    });
  };

  /** 导入时的"登记为"候选：**来自后端 options**（唯一真相）。只剔掉 `sheet` ——
   *  "2x2 四视图定妆照"是一张拼版图，当人物参考图喂给 i2v 会把网格与分格布局
   *  复刻进画面。没有生成器的类型标上"（仅导入）"，用户才知道它不能在这里出图。 */
  const importKindOptions = (options.image_kinds
    || Object.entries(KIND_LABELS_FALLBACK)
      .map(([value, label]) => ({ value, label, generatable: value !== 'variant' })))
    .filter((k: Json) => k.value !== 'sheet')
    .map((k: Json) => ({
      value: k.value,
      label: k.generatable === false ? `${k.label}（仅导入）` : k.label,
    }));

  const galleryGroups = gallery ? groupImages(gallery.t) : [];

  /** 向导里正在编辑的那位主播。出图后就地给一个"看刚生成的那张"的入口 ——
   *  否则用户在向导里按下生成、看到"已登记"三个字，图却不在眼前。 */
  const wizardTalent = wizardTid != null
    ? talents.find((x: Json) => x.id === wizardTid)
    : undefined;

  // ---------------- 向导 ----------------

  const openCreate = () => {
    setWizardMode('create');
    setWizardTid(null);
    setStep(0);
    setForm({ name: '', age: 27, gender: 'female', nationality: '中国',
              description: '', voice_style: '', appearance: '', hint: '',
              lang: 'en', preset: '', spec: {} });
    setGenResult('');
    setWizardOpen(true);
  };

  const openAppearance = (t: Json) => {
    setWizardMode('appearance');
    setWizardTid(t.id);
    setStep(0);
    setForm({
      name: t.name || '', age: t.profile?.age ?? 27,
      gender: t.profile?.gender || 'female',
      nationality: t.profile?.nationality || '中国',
      description: t.description || '', voice_style: t.voice_style || '',
      appearance: t.appearance || '', hint: '', lang: 'en',
      // 回显上次的选择，用户才能在这个基础上继续调，而不是从头再来
      preset: t.preset || '', spec: t.spec || {},
    });
    setGenResult(t.appearance || '');
    setWizardOpen(true);
  };

  /** 选一个人设预设：整组替换为该预设的维度值（随后可逐项覆盖）。
   *  传空串则只清空 preset 标记、保留已手动选的维度。 */
  const applyPreset = (value: string) => {
    if (!value) {
      setForm((f: Json) => ({ ...f, preset: '' }));
      return;
    }
    const p = (options.presets || []).find((x: Json) => x.value === value);
    setForm((f: Json) => ({
      ...f, preset: value, spec: { ...(p?.spec || {}) },
    }));
  };

  const setSpec = (key: string, value: string | undefined) => {
    setForm((f: Json) => {
      const next = { ...(f.spec || {}) };
      if (value) next[key] = value; else delete next[key];
      return { ...f, spec: next };
    });
  };

  const closeWizard = () => {
    setWizardOpen(false);
    setWizardTid(null);
    setStep(0);
  };

  /** 调文本大模型扩写形象提示词。
   *  `mode='draft'`：按当前所选维度拼装后润色；
   *  `mode='vary'`：先换掉真实组成成分（脸型/服装等）再拼装润色，得到"另一版"。
   *  始终把当前 appearance 当作上一版上下文回传，避免覆盖已有的细节。 */
  const genAppearance = async (mode: 'draft' | 'vary') => {
    if (genning) return;
    setGenning(true);
    try {
      const r = await api.previewTalentAppearance({
        name: form.name, age: form.age, gender: form.gender,
        nationality: form.nationality,
        appearance: form.appearance || '',          // 上一版，作为 previous 上下文
        hint: form.hint || '', lang: form.lang || 'en',
        spec: form.spec || {},                       // 全部维度的选择
        preset: form.preset || '',
        vary: mode === 'vary' ? 'full' : '',
        seed: Date.now() % 100000,
      });
      // 后端若触发了 vary，会回传新的 spec —— 同步回下拉框，让界面与实际一致。
      // 同时把这一版（提示词 + 形象设定）落库：队列读的是库里的 appearance / spec。
      // 注意不能用 setForm 之后的 form 闭包去存——那一帧还是旧值，必须拿响应体存。
      const nextSpec = r.spec || form.spec;
      if (wizardTid != null) {
        await api.updateTalent(wizardTid, {
          appearance: r.appearance,
          appearance_source: 'llm',
          spec: nextSpec,
          preset: form.preset || '',
        }).catch((e) => message.error((e as Error).message));
      }
      setForm((f: Json) => ({ ...f, appearance: r.appearance, spec: nextSpec }));
      setGenResult(r.appearance);
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setGenning(false);
    }
  };

  /** 第 1 步 → 第 2 步：基本信息齐了就先落库。
   *  出图必须有 talent id，所以建库放在这一步，而不是等到最后"完成"——
   *  否则用户生成了半天提示词，一点"取消"就全没了。 */
  const goStep2 = async () => {
    if (!form.name.trim()) { message.warning('名称必填'); return; }
    if (form.age == null) { message.warning('年龄必填'); return; }
    if (!form.gender || !form.nationality) { message.warning('性别与国籍必填'); return; }
    if (busy) return;
    setBusy(true);
    try {
      const t = await api.createTalent({
        name: form.name.trim(), age: form.age, gender: form.gender,
        nationality: form.nationality,
        description: form.description || '', voice_style: form.voice_style || '',
        appearance: form.appearance || '',
        appearance_source: sourceOf(),
      });
      setWizardTid(t.id);
      setStep(1);
      setNotice(`主播已建档（${t.code}），下一步把形象定下来`);
      void load();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const sourceOf = () => {
    const a = (form.appearance || '').trim();
    if (!a) return '';
    return a === genResult && genResult ? 'llm' : 'manual';
  };

  /** 把当前向导里的全部可编辑字段（提示词 + 形象设定）落库。
   *  出图前必须先落库：队列读的是数据库里的 appearance / spec，没存就白生成。
   *  返回是否成功（失败已弹错，调用方可据此决定是否继续）。 */
  const persistAll = async (): Promise<boolean> => {
    if (wizardTid == null || busy) return false;
    setBusy(true);
    try {
      await api.updateTalent(wizardTid, {
        appearance: form.appearance || '',
        appearance_source: sourceOf(),
        spec: form.spec || {},
        preset: form.preset || '',
      });
      return true;
    } catch (e) {
      message.error((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const saveAppearance = async () => {
    if (!(await persistAll())) return;
    message.success('形象提示词已更新');
    closeWizard();
    void load();
  };

  /** 形象设定 → 下一步：先把 spec/preset 落库，再切到提示词那一步。 */
  const goStepSpec = async () => {
    if (!(await persistAll())) return;
    setStep(wizardMode === 'create' ? 2 : 1);
  };

  /** 向导内出图：先确保 appearance/spec 已落库，再派任务。 */
  const renderFromWizard = async (kind: string, label: string) => {
    if (wizardTid == null) return;
    if (!(await persistAll())) return;
    await renderImage(wizardTid, kind, label);
  };

  // ---------------- 出图 ----------------

  const renderImage = async (tid: number, kind: string, label: string) => {
    const key = `${tid}:${kind}`;
    if (rendering.includes(key)) return;
    setRendering((p) => [...p, key]);
    setNotice(`正在生成${label}…`);
    try {
      const job = await api.renderTalentImage(tid, { kind });
      let last = job;
      // 单图生图通常 30–90s，给 5 分钟余量；超时不报错 —— 任务还在队列里，
      // 刷新页面也能从任务台账找回，绝不能因为前端超时就去重提（会重复计费）
      const deadline = Date.now() + 5 * 60 * 1000;
      while (Date.now() < deadline) {
        if (last.status === 'succeeded' || last.status === 'failed') break;
        await new Promise((r) => setTimeout(r, 3000));
        last = await api.getJob(job.job_id);
      }
      if (last.status === 'succeeded' && last.output_path) {
        await api.addTalentImage(tid, { kind, path: last.output_path });
        setNotice(`${label}已生成并登记到主播库`);
        void load();
      } else if (last.status === 'failed') {
        setNotice(`${label}生成失败：${last.error || last.message || '未知原因'}`);
      } else {
        setNotice(`${label}仍在后台执行，可稍后刷新查看；不会重复计费`);
      }
    } catch (e) {
      setNotice(`${label}生成失败：${(e as Error).message}`);
    } finally {
      setRendering((p) => p.filter((k) => k !== key));
    }
  };

  // ---------------- 导入已有图片 ----------------

  const addFiles = (files: FileList | null) => {
    if (!files) return;
    setImportFiles((prev) => [
      ...prev, ...Array.from(files).filter((f) => f.type.startsWith('image/')),
    ].slice(0, 5));
  };

  const runImport = async () => {
    if (!importFor || !importFiles.length || importing) return;
    setImporting(true);
    try {
      const r = await api.uploadImages('characters', String(importFor.id), importFiles);
      for (const url of r.urls) {
        await api.addTalentImage(importFor.id, { kind: importKind, path: url });
      }
      message.success(`已导入 ${r.urls.length} 张图片`);
      setImportFor(null);
      setImportFiles([]);
      void load();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setImporting(false);
    }
  };

  // ---------------- 列表操作 ----------------

  const remove = (t: Json) => {
    modal.confirm({
      title: `删除主播「${t.name}」？`,
      content: '只删记录，磁盘上的参考图保留。',
      okButtonProps: { danger: true },
      onOk: async () => {
        try { await api.deleteTalent(t.id); message.success('已删除'); void load(); }
        catch (e) { message.error((e as Error).message); }
      },
    });
  };

  /** 编辑只管基本信息；形象提示词有自己的流程（要生成/预览，混在表单里会很挤）。
   *  **必须用真 Modal + state**：`modal.confirm` 的 content 只渲染一次，
   *  用闭包变量做受控输入的话，键盘敲进去不会重渲染，框里始终是最初的值。 */
  const [editFor, setEditFor] = useState<Json | null>(null);
  const [editForm, setEditForm] = useState<Json>({});
  const [savingEdit, setSavingEdit] = useState(false);

  const openEdit = (t: Json) => {
    setEditFor(t);
    setEditForm({
      name: t.name || '', age: t.profile?.age ?? 27,
      gender: t.profile?.gender || 'female',
      nationality: t.profile?.nationality || '中国',
      voice_style: t.voice_style || '', description: t.description || '',
    });
  };

  const saveEdit = async () => {
    if (!editFor || savingEdit) return;
    setSavingEdit(true);
    try {
      await api.updateTalent(editFor.id, {
        name: (editForm.name || '').trim(), age: editForm.age,
        gender: editForm.gender, nationality: editForm.nationality,
        voice_style: (editForm.voice_style || '').trim(),
        description: (editForm.description || '').trim(),
      });
      message.success('已保存');
      setEditFor(null);
      void load();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setSavingEdit(false);
    }
  };

  return (
    <div className="cf-grid-bg" style={{ minHeight: '100%', padding: '28px 24px 48px' }}>
      <div style={{ maxWidth: 980, margin: '0 auto' }}>
        <div style={{
          marginBottom: 20, display: 'flex', alignItems: 'flex-start',
          justifyContent: 'space-between', gap: 16,
        }}>
          <div>
            <h1 className="cf-page-title">主播库</h1>
            <p className="cf-page-sub" style={{ marginBottom: 0 }}>
              填基本信息 → 选形象设定 → 用文本大模型写出形象提示词 → 满意了再出图
            </p>
          </div>
          {/* 新建入口放页首：原先在最底部、要把整库模特图滚完才看得到，
              库一大就等于找不到入口（R-32 用户反馈）。 */}
          <Button className="cf-brand-gradient" icon={<PlusOutlined />}
            onClick={openCreate} style={{ flexShrink: 0 }}>
            新建主播
          </Button>
        </div>

        <div className="cf-card" style={{ padding: 14, marginBottom: 16 }}>
          <p className="cf-hint" style={{ fontSize: 13 }}>
            年龄、性别、国籍是选角的三个硬维度，<b>必填</b>——缺一个，模型就只能靠猜，
            之后每一版提示词都在漂。<b>「形象设定」那一步</b>把脸型、服装、布光等
            十六个维度显式化：你可以一键套人设预设，也可逐项点选，模型不再随机发挥。
            不满意就手动改或换一版，满意了再出图（出图才花钱）。
            已有模特图的，直接导入即可，同样留在主播库里复用。
          </p>
        </div>

        {notice && (
          <div className="cf-card" style={{
            padding: '10px 14px', marginBottom: 16, fontSize: 12,
            color: 'var(--cf-text-dim)',
          }}>
            {notice}
          </div>
        )}

        <Space direction="vertical" size={12} style={{ width: '100%', marginBottom: 16 }}>
          {talents.map((t) => {
            const busyFront = rendering.includes(`${t.id}:front`);
            const busyThree = rendering.includes(`${t.id}:threeview`);
            const busyPoster = rendering.includes(`${t.id}:poster`);
            return (
              <div className="cf-card" key={t.id}>
                <div style={{ padding: 14, display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                  {t.cover_url ? (
                    <div style={{ position: 'relative', flexShrink: 0 }}>
                      <button
                        title={`查看全部 ${imgCount(t)} 张图`}
                        onClick={() => openGallery(t)}
                        style={{
                          border: '1px solid var(--cf-border)', borderRadius: 8, padding: 0,
                          background: 'none', cursor: 'zoom-in', lineHeight: 0,
                        }}>
                        <img src={t.cover_url} alt={`${t.name} 参考图`}
                          style={{ width: 64, height: 64, objectFit: 'cover', borderRadius: 7 }} />
                      </button>
                      {imgCount(t) > 1 && (
                        <span style={{
                          position: 'absolute', right: -4, bottom: -4, fontSize: 11,
                          padding: '0 6px', height: 18, lineHeight: '18px', borderRadius: 9,
                          background: '#c08b52', color: '#1a1410', fontWeight: 500,
                        }}>+{imgCount(t) - 1}</span>
                      )}
                    </div>
                  ) : (
                    <div style={{
                      width: 40, height: 40, flexShrink: 0, borderRadius: '50%',
                      background: 'rgba(192,139,82,0.14)', color: '#c08b52',
                      display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 18,
                    }}><UserOutlined /></div>
                  )}

                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                      <span style={{ fontSize: 14, fontWeight: 600 }}>{t.name}</span>
                      <Tag style={{ margin: 0, fontSize: 11 }}>{t.code}</Tag>
                      {t.profile?.age != null && (
                        <span style={tagStyle}>{t.profile.age} 岁</span>
                      )}
                      {t.profile?.gender && (
                        <span style={tagStyle}>
                          {options.genders.find((g: Json) => g.value === t.profile.gender)?.label
                            || t.profile.gender}
                        </span>
                      )}
                      {t.profile?.nationality && (
                        <span style={tagStyle}>{t.profile.nationality}</span>
                      )}
                    </div>

                    {t.appearance ? (
                      <p style={{
                        fontSize: 12, color: 'var(--cf-text-faint)', margin: '0 0 4px',
                        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                      }} title={t.appearance}>
                        形象：{t.appearance}
                        {t.appearance_source === 'llm' && (
                          <span style={{ color: '#c08b52' }}> · 模型生成</span>
                        )}
                      </p>
                    ) : (
                      <p style={{ fontSize: 12, color: 'var(--cf-text-faint)', margin: '0 0 4px' }}>
                        还没有形象提示词 —— 点「形象提示词」生成
                      </p>
                    )}

                    <Space size={4} wrap>
                      {groupImages(t).map(({ kind, items }) => (
                        <button key={kind} type="button"
                          title={`查看${kindLabel(kind)}（${items.length} 张）`}
                          onClick={() => openGallery(t, kind)}
                          style={kindChipStyle}>
                          {kindLabel(kind)}{items.length > 1 ? ` ×${items.length}` : ''} ✓
                        </button>
                      ))}
                      {t.voice_style && <span style={tagStyle}>声线：{t.voice_style}</span>}
                    </Space>
                  </div>

                  <Space size={2} wrap style={{ maxWidth: 320, justifyContent: 'flex-end' }}>
                    <Button type="link" size="small" disabled={!imgCount(t)}
                      title={imgCount(t) ? '查看这位主播的全部形象图' : '还没有任何图'}
                      onClick={() => openGallery(t)}>
                      查看图（{imgCount(t)}）
                    </Button>
                    <Button type="link" size="small" onClick={() => openAppearance(t)}>
                      形象提示词
                    </Button>
                    <Button type="link" size="small" disabled={busyFront}
                      onClick={() => renderImage(t.id, 'front', '正面图')}>
                      {busyFront ? <><LoadingOutlined /> 出图中…</> : '生成正面图'}
                    </Button>
                    <Button type="link" size="small" disabled={busyThree}
                      onClick={() => renderImage(t.id, 'threeview', '人物三视图')}>
                      {busyThree ? <><LoadingOutlined /> 出图中…</> : '生成三视图'}
                    </Button>
                    <Button type="link" size="small" disabled={busyPoster}
                      onClick={() => renderImage(t.id, 'poster', '氛围图')}
                      title="采用画面层（场景/光线/姿势/构图/色调）生成带场景的成品图">
                      {busyPoster ? <><LoadingOutlined /> 出图中…</> : '生成氛围图'}
                    </Button>
                    <Button type="text" size="small" icon={<UploadOutlined />}
                      title="导入已有的模特图片"
                      onClick={() => { setImportKind('front'); setImportFor(t); }} />
                    <Button type="text" size="small" icon={<EditOutlined />}
                      title="编辑基本信息" onClick={() => openEdit(t)} />
                    <Button type="text" size="small" danger icon={<DeleteOutlined />}
                      onClick={() => remove(t)} />
                  </Space>
                </div>
              </div>
            );
          })}
        </Space>

        {loading && (
          <Typography.Text type="secondary"><LoadingOutlined /> 加载中…</Typography.Text>
        )}
      </div>

      {/* ---------------- 新建 / 改提示词 向导 ---------------- */}
      <Modal open={wizardOpen} width={720} footer={null}
        title={wizardMode === 'create' ? '新建主播' : '形象提示词'}
        onCancel={closeWizard}>
        <Steps size="small" style={{ margin: '8px 0 18px' }} current={step}
          items={wizardMode === 'create'
            ? [{ title: '基本信息' }, { title: '形象设定' }, { title: '形象提示词与出图' }]
            : [{ title: '形象设定' }, { title: '形象提示词' }]} />

        {/* 第 1 步：基本信息 */}
        {wizardMode === 'create' && step === 0 && (
          <div style={{ display: 'grid', gap: 12 }}>
            <div>
              <label style={labelStyle}>名称 <span style={reqStyle}>必填</span></label>
              <Input value={form.name} placeholder="例如：小雅"
                onChange={(e) => setForm((f: Json) => ({ ...f, name: e.target.value }))} />
            </div>
            <Space size={10} wrap>
              <div>
                <label style={labelStyle}>年龄 <span style={reqStyle}>必填</span></label>
                <InputNumber min={1} max={120} value={form.age}
                  onChange={(v) => setForm((f: Json) => ({ ...f, age: v ?? undefined }))} />
              </div>
              <div>
                <label style={labelStyle}>性别 <span style={reqStyle}>必填</span></label>
                <Select value={form.gender} style={{ width: 120 }} options={options.genders}
                  onChange={(v) => setForm((f: Json) => ({ ...f, gender: v }))} />
              </div>
              <div>
                <label style={labelStyle}>国籍 <span style={reqStyle}>必填</span></label>
                <Select value={form.nationality} style={{ width: 160 }}
                  showSearch
                  options={options.nationalities.map((n: string) => ({ value: n, label: n }))}
                  onChange={(v) => setForm((f: Json) => ({ ...f, nationality: v }))} />
              </div>
            </Space>
            <div>
              <label style={labelStyle}>声线风格（选填）</label>
              <Input value={form.voice_style} placeholder="例如：温柔知性、语速偏慢"
                onChange={(e) => setForm((f: Json) => ({ ...f, voice_style: e.target.value }))} />
            </div>
            <div>
              <label style={labelStyle}>人物设定（选填）</label>
              <Input value={form.description} placeholder="一句话人设，例如：都市白领，干练温和"
                onChange={(e) => setForm((f: Json) => ({ ...f, description: e.target.value }))} />
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <Button onClick={closeWizard}>取消</Button>
              <Button className="cf-brand-gradient" onClick={() => void goStep2()}
                disabled={!form.name.trim() || form.age == null || !form.gender
                  || !form.nationality}>
                下一步：形象设定
              </Button>
            </div>
          </div>
        )}

        {/* 第 2 步（create）/ 第 1 步（appearance）：形象设定 */}
        {((wizardMode === 'create' && step === 1) || (wizardMode === 'appearance' && step === 0)) && (
          <div style={{ display: 'grid', gap: 12 }}>
            <p className="cf-hint" style={{ fontSize: 13, marginBottom: 0 }}>
              这一步零成本——把脸型、服装、布光这些原本靠模型随机发挥的变量先定下来，
              后面生成的提示词和参考图才会一致、可控。
            </p>
            <SpecEditor
              options={options}
              preset={form.preset}
              spec={form.spec || {}}
              onPreset={applyPreset}
              onPick={setSpec}
            />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <Button onClick={closeWizard}>取消</Button>
              <Button className="cf-brand-gradient" loading={busy}
                onClick={() => void goStepSpec()}>
                {wizardMode === 'create' ? '下一步：生成提示词' : '下一步'}
              </Button>
            </div>
          </div>
        )}

        {/* 第 3 步（create）/ 第 2 步（appearance）：形象提示词与出图 */}
        {((wizardMode === 'create' && step === 2) || (wizardMode === 'appearance' && step === 1)) && (
          <div style={{ display: 'grid', gap: 12 }}>
            <div>
              <label style={labelStyle}>补充要求（选填，只影响生成）</label>
              <Input value={form.hint} placeholder="例如：看起来更干练、偏运动感"
                onChange={(e) => setForm((f: Json) => ({ ...f, hint: e.target.value }))} />
            </div>
            <Space size={8} wrap>
              <Button icon={<ThunderboltOutlined />} loading={genning}
                onClick={() => void genAppearance('draft')}>
                用文本大模型生成
              </Button>
              <Button disabled={!form.appearance || genning}
                onClick={() => void genAppearance('vary')}>
                换一版形象
              </Button>
              <Select value={form.lang} style={{ width: 130 }}
                options={[{ value: 'en', label: '英文（生图更稳）' },
                          { value: 'zh', label: '中文' }]}
                onChange={(v) => setForm((f: Json) => ({ ...f, lang: v }))} />
            </Space>
            <div>
              <label style={labelStyle}>
                形象提示词（可手动修改）
                {form.appearance && form.appearance === genResult && genResult && (
                  <span style={{ color: '#c08b52' }}> · 模型生成</span>
                )}
                {form.appearance && form.appearance !== genResult && (
                  <span style={{ color: 'var(--cf-text-faint)' }}> · 已手改</span>
                )}
              </label>
              <Input.TextArea value={form.appearance} rows={4}
                placeholder="点上面的按钮让模型生成，也可以自己写"
                onChange={(e) => setForm((f: Json) => ({ ...f, appearance: e.target.value }))} />
              <p className="cf-hint" style={{ marginTop: 4 }}>
                这段是文本大模型在「形象设定」基础上润色出的完整人物描述，会逐字复用到每一个镜头，
                也作为生图时的唯一人物描述。可以手动微调，但越具体越稳。
              </p>
            </div>

            {wizardMode === 'create' && wizardTid != null && (
              <div className="cf-card" style={{ padding: 14 }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
                  用这张脸出图（会花钱）
                </div>
                <Space size={8} wrap>
                  <Button disabled={!form.appearance || rendering.includes(`${wizardTid}:front`)}
                    loading={rendering.includes(`${wizardTid}:front`)}
                    onClick={() => void renderFromWizard('front', '正面图')}>
                    生成正面图
                  </Button>
                  <Button
                    disabled={!form.appearance || rendering.includes(`${wizardTid}:threeview`)}
                    loading={rendering.includes(`${wizardTid}:threeview`)}
                    onClick={() => void renderFromWizard('threeview', '人物三视图')}>
                    生成人物三视图
                  </Button>
                  <Button type="text" icon={<UploadOutlined />}
                    onClick={() => { setImportKind('front'); setImportFor({ id: wizardTid }); }}>
                    我已有图片，直接导入
                  </Button>
                </Space>
                {wizardTalent && imgCount(wizardTalent) > 0 && (
                  <p className="cf-hint" style={{ marginTop: 8, marginBottom: 0 }}>
                    这位主播已登记 {imgCount(wizardTalent)} 张图 ——{' '}
                    <Button type="link" size="small"
                      style={{ padding: 0, height: 'auto' }}
                      onClick={() => openGallery(wizardTalent)}>
                      打开查看
                    </Button>
                  </p>
                )}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              {wizardMode === 'appearance' && (
                <>
                  <Button onClick={closeWizard}>取消</Button>
                  <Button className="cf-brand-gradient" loading={busy}
                    onClick={() => void saveAppearance()}>
                    保存提示词
                  </Button>
                </>
              )}
              {wizardMode === 'create' && (
                <Button className="cf-brand-gradient" loading={busy}
                  onClick={async () => { if (await persistAll()) closeWizard(); void load(); }}>
                  完成
                </Button>
              )}
            </div>
          </div>
        )}
      </Modal>

      {/* ---------------- 编辑基本信息 ---------------- */}
      <Modal open={editFor !== null} title={`编辑「${editFor?.name || ''}」基本信息`}
        okText="保存" confirmLoading={savingEdit}
        onCancel={() => setEditFor(null)} onOk={() => void saveEdit()}>
        <div style={{ display: 'grid', gap: 12 }}>
          <div>
            <label style={labelStyle}>名称 <span style={reqStyle}>必填</span></label>
            <Input value={editForm.name}
              onChange={(e) => setEditForm((f: Json) => ({ ...f, name: e.target.value }))} />
          </div>
          <Space size={10} wrap>
            <div>
              <label style={labelStyle}>年龄 <span style={reqStyle}>必填</span></label>
              <InputNumber min={1} max={120} value={editForm.age}
                onChange={(v) => setEditForm((f: Json) => ({ ...f, age: v ?? undefined }))} />
            </div>
            <div>
              <label style={labelStyle}>性别 <span style={reqStyle}>必填</span></label>
              <Select value={editForm.gender} style={{ width: 120 }} options={options.genders}
                onChange={(v) => setEditForm((f: Json) => ({ ...f, gender: v }))} />
            </div>
            <div>
              <label style={labelStyle}>国籍 <span style={reqStyle}>必填</span></label>
              <Select value={editForm.nationality} style={{ width: 160 }} showSearch
                options={options.nationalities.map((n: string) => ({ value: n, label: n }))}
                onChange={(v) => setEditForm((f: Json) => ({ ...f, nationality: v }))} />
            </div>
          </Space>
          <div>
            <label style={labelStyle}>声线风格</label>
            <Input value={editForm.voice_style} placeholder="例如：温柔知性、语速偏慢"
              onChange={(e) =>
                setEditForm((f: Json) => ({ ...f, voice_style: e.target.value }))} />
          </div>
          <div>
            <label style={labelStyle}>人物设定</label>
            <Input value={editForm.description}
              onChange={(e) =>
                setEditForm((f: Json) => ({ ...f, description: e.target.value }))} />
          </div>
        </div>
      </Modal>

      {/* ---------------- 导入已有图片 ---------------- */}
      <Modal open={importFor !== null} title="导入已有的模特图片" okText="导入"
        confirmLoading={importing} okButtonProps={{ disabled: !importFiles.length }}
        onCancel={() => { setImportFor(null); setImportFiles([]); }}
        onOk={() => void runImport()}>
        <div style={{ display: 'grid', gap: 10 }}>
          <div>
            <label style={labelStyle}>登记为</label>
            <Select value={importKind} style={{ width: 220 }}
              options={importKindOptions}
              onChange={setImportKind} />
          </div>
          <div
            className={`cf-drop${dragging ? ' is-over' : ''}`}
            onClick={() => fileRef.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files);
            }}
            style={{ padding: '18px 12px', textAlign: 'center', cursor: 'pointer' }}>
            <div className="cf-drop-icon"><InboxOutlined /></div>
            <div style={{ fontSize: 13 }}>点击或把图片拖进来（最多 5 张）</div>
            <input ref={fileRef} type="file" accept="image/*" multiple hidden
              onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }} />
          </div>
          {importFiles.length > 0 && (
            <div style={{ fontSize: 12, color: 'var(--cf-text-dim)' }}>
              已选 {importFiles.length} 张：{importFiles.map((f) => f.name).join('、')}
            </div>
          )}
        </div>
      </Modal>

      {/* ---------------- 形象图画廊 ----------------
          这四类图生成的**入口**在卡片上，但原来除封面外没有任何**查看**入口：
          「三视图 ✓」只是个死标签，氛围图更是连缩略图都没有。这里把某位主播的
          全部图一次铺开，按类型分组，并说明每类图在下游的用途。 */}
      <Modal open={gallery !== null} footer={null} width={880}
        title={gallery
          ? `${gallery.t.name} 的形象图（共 ${imgCount(gallery.t)} 张）`
          : ''}
        onCancel={() => setGallery(null)}>
        {gallery && !galleryGroups.length && (
          <p className="cf-hint" style={{ fontSize: 13 }}>
            还没有任何图。关掉本窗口后，在卡片上用「生成正面图 / 生成三视图 / 生成氛围图」
            各出一张，或点上传图标导入已有的模特图。
          </p>
        )}
        {gallery && galleryGroups.length > 0 && (
          <div style={{
            display: 'grid', gap: 18, maxHeight: '62vh',
            overflowY: 'auto', paddingRight: 6,
          }}>
            {galleryGroups.map(({ kind, items }) => (
              <div key={kind} style={
                gallery.focus === kind
                  ? { border: '1px solid rgba(192,139,82,0.45)', borderRadius: 10, padding: 10 }
                  : undefined
              }>
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
                  marginBottom: 8,
                }}>
                  <span style={{ fontSize: 13, fontWeight: 600 }}>{kindLabel(kind)}</span>
                  <span style={tagStyle}>{items.length} 张</span>
                  <span style={{ fontSize: 11, color: 'var(--cf-text-faint)' }}>
                    {isRenderKind(kind)
                      ? '成品图：采用画面层（场景 / 光线 / 姿势 / 构图 / 色调），不进 i2v 参考图'
                      : '参考图：中性棚拍，出片时按顺序喂给模型'}
                  </span>
                </div>
                <div style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
                  gap: 10,
                }}>
                  {items.map((im: Json, i: number) => (
                    <figure key={im.id} style={{ margin: 0 }}>
                      <button type="button" title="点击查看大图"
                        onClick={() => setZoom({
                          imgs: items, idx: i,
                          name: `${gallery.t.name} · ${kindLabel(kind)}`,
                        })}
                        style={{
                          position: 'relative', display: 'block', width: '100%', padding: 0,
                          border: '1px solid var(--cf-border)', borderRadius: 8,
                          overflow: 'hidden', background: 'none',
                          cursor: 'zoom-in', lineHeight: 0,
                        }}>
                        <img src={im.url} alt={`${kindLabel(kind)} ${i + 1}`}
                          style={{
                            width: '100%', aspectRatio: '3 / 4', objectFit: 'cover',
                            display: 'block',
                          }} />
                        {!im.exists && (
                          <span style={{
                            position: 'absolute', left: 0, right: 0, bottom: 0,
                            padding: '3px 6px', fontSize: 11, lineHeight: 1.4,
                            background: 'rgba(163,45,45,0.88)', color: '#fff',
                          }}>文件已丢失（只剩登记记录）</span>
                        )}
                      </button>
                      <figcaption style={{
                        display: 'flex', alignItems: 'center',
                        justifyContent: 'space-between', gap: 6, marginTop: 4,
                      }}>
                        <span style={{ fontSize: 11, color: 'var(--cf-text-faint)' }}>
                          #{im.id}{im.variant_name ? ` · ${im.variant_name}` : ''}
                        </span>
                        <Button type="text" size="small" danger icon={<DeleteOutlined />}
                          title="只删这条登记记录，磁盘文件保留"
                          onClick={() => removeImage(im)} />
                      </figcaption>
                    </figure>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Modal>

      {/* ---------------- 大图查看（可翻页） ---------------- */}
      <Modal open={zoom !== null} footer={null} width={720}
        title={zoom ? `${zoom.name}（${zoom.idx + 1} / ${zoom.imgs.length}）` : ''}
        onCancel={() => setZoom(null)}>
        {zoom && (
          <div>
            <img src={zoom.imgs[zoom.idx]?.url} alt={zoom.name}
              style={{ width: '100%', borderRadius: 8 }} />
            {zoom.imgs.length > 1 && (
              <div style={{
                display: 'flex', justifyContent: 'space-between', marginTop: 10,
              }}>
                <Button disabled={zoom.idx === 0}
                  onClick={() => setZoom((z) => (z ? { ...z, idx: z.idx - 1 } : z))}>
                  上一张
                </Button>
                <Button disabled={zoom.idx >= zoom.imgs.length - 1}
                  onClick={() => setZoom((z) => (z ? { ...z, idx: z.idx + 1 } : z))}>
                  下一张
                </Button>
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

/** 形象设定编辑器：人设预设 + 按层分组的维度下拉。
 *
 *  全部选项从后端 `/api/talents/options` 的 `layers / dims / presets` 来，
 *  前端不抄词表——改词表两边同时生效，不会出现"界面有、后端不认"。
 *
 *  维度分三层：
 *    identity  身份（脸型/肤色/身材/气质/神情）—— 跨图跨镜必须一致
 *    styling   造型（服装/妆容/发型/发色/配饰）—— 换装即新变体
 *    render    画面（场景/光线/姿势/构图/色调/风格）—— 只对氛围图生效
 */
function SpecEditor({ options, preset, spec, onPreset, onPick }: {
  options: Json;
  preset: string;
  spec: Json;
  onPreset: (v: string) => void;
  onPick: (k: string, v?: string) => void;
}) {
  const dimsByKey: Record<string, Json> = {};
  (options.dims || []).forEach((d: Json) => { dimsByKey[d.key] = d; });
  const presetOpts = (options.presets || []).map((p: Json) => ({
    value: p.value,
    label: `${p.label}　·　${p.fits}`,
  }));
  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <div>
        <label style={labelStyle}>人设预设（选填，一键铺满后逐项覆盖）</label>
        <Select
          allowClear
          placeholder="先挑一个方向，再微调；或留空完全自己选"
          style={{ width: '100%' }}
          value={preset || undefined}
          options={presetOpts}
          onChange={(v) => onPreset(v || '')}
        />
        <p className="cf-hint" style={{ marginTop: 4 }}>
          预设会把服装、场景、光线等一次性填满，你可以在下面逐项改。切换预设会整组替换当前选择。
        </p>
      </div>

      <Collapse
        defaultActiveKey={['identity', 'styling', 'render']}
        items={(options.layers || []).map((layer: Json) => ({
          key: layer.key,
          label: layer.label,
          children: (
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))',
              gap: 12,
            }}>
              {layer.dims.map((key: string) => {
                const dim = dimsByKey[key];
                if (!dim) return null;
                return (
                  <div key={key}>
                    <label style={labelStyle}>{dim.label}</label>
                    <Select
                      allowClear
                      size="small"
                      style={{ width: '100%' }}
                      placeholder="默认（不指定）"
                      value={spec[key] || undefined}
                      options={[
                        { value: '', label: '（不指定）' },
                        ...dim.options.map((o: Json) => ({ value: o.value, label: o.label })),
                      ]}
                      onChange={(v) => onPick(key, v || undefined)}
                    />
                  </div>
                );
              })}
            </div>
          ),
        }))}
      />

      <div className="cf-card" style={{
        padding: 10, fontSize: 12, color: 'var(--cf-text-faint)',
      }}>
        画面层（场景 / 光线 / 姿势 / 构图 / 色调 / 风格）只对<b>氛围图</b>生效，
        不会写进正面图与三视图——否则背景会被下游每一支成片原样复刻。
        想拍带场景的成品图，请用「生成氛围图」。
      </div>
    </div>
  );
}

const labelStyle: React.CSSProperties = {
  fontSize: 12, color: 'var(--cf-text-dim)', display: 'block', marginBottom: 4,
};
const reqStyle: React.CSSProperties = { color: '#c08b52', fontSize: 11 };
const tagStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', fontSize: 11, padding: '0 8px',
  borderRadius: 10, height: 18, background: 'rgba(255,255,255,0.05)',
  color: 'var(--cf-text-dim)',
};
/** 可点击的图类型标签。用 `<button>` 而不是 `<span>` —— 原来这几个 `✓` 是死标签，
 *  用户看到"三视图 ✓"却点不动，正是"没有入口"的来源。 */
const kindChipStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', fontSize: 11, padding: '0 8px',
  borderRadius: 10, height: 18, background: 'rgba(255,255,255,0.05)',
  color: 'var(--cf-text-dim)', border: '1px solid var(--cf-border)',
  cursor: 'pointer', font: 'inherit',
};
