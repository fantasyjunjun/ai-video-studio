import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  App as AntApp, Button, Input, Select, Space, Typography,
} from 'antd';
import {
  CloseOutlined, DeleteOutlined, EditOutlined, ExclamationCircleOutlined,
  InboxOutlined, LinkOutlined, LoadingOutlined, PictureOutlined, PlusOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons';
import { api, type Json } from '../api';
import { CATEGORY_OPTIONS, categoryMeta } from '../categories';

/** 商品图上限。前端拦一次、后端再拦一次 —— 前端只是体验，后端才是约束。 */
const MAX_IMAGES = 5;

type DraftImage = { id: string; url: string; file?: File; role?: string; roleTouched?: boolean };

/** 图片用途：默认由**视觉大模型在生成视频脚本时自动识别**（R-20：保存不再识别——
 *  上传即识别既拖慢保存、又在用户还没决定用时提前花钱；识别一次后随商品缓存复用）。
 *  R-27 恢复手工下拉：AI 判错要有纠错通道（实测把香调图判成主体、把产品判成信息图）。
 *  **手工优先**：动过任意下拉就整组显式传 image_meta，后端"显式给就尊重"；
 *  AI 识别缓存只补画面描述（desc）、不会覆盖手工定的用途。一张都没动 → 不传，
 *  保持纯 AI 识别，也不把已缓存的 desc 抹掉（image_meta 归一只留 role）。
 *  只有「主体图」会作为参考图发给模型；信息图/海报喂进去会把版式、第二只型号
 *  复刻进画面。同一实物的多角度照都是主体图（帮模型锁外形）；不同型号/花色请
 *  拆成独立商品 —— 一物一档。 */
/** 下拉选项（与后端 refs.PRODUCT_ROLE_LABELS 对齐）。 */
const ROLE_OPTIONS = [
  { value: 'hero', label: '主体图（作为参考图）' },
  { value: 'infographic', label: '信息图（不进画面）' },
  { value: 'poster', label: '海报/场景图（不进画面）' },
  { value: 'other', label: '其他（不进参考图）' },
];

/** 老数据兼容：R-16 的「瓶身照」值 bottle 在 R-17 统一为 hero（语义等价）。 */
const normRole = (role?: string | null) => (role === 'bottle' ? 'hero' : role || 'hero');

const uid = () =>
  (globalThis.crypto?.randomUUID?.() ?? `d-${Date.now()}-${Math.random().toString(36).slice(2)}`);

/** 商品库：卡片网格 + 拖拽上传 + 贴链接导入。
 *
 *  与 ClipForge 的 `/products` 对齐（1:1）。三个关键设计：
 *
 *  1. **图片先上传、后入库**。表单打开时先生成一个 `draftId`，图片按它落盘；
 *     保存时才把返回的 URL 写进商品记录。原因是商品在我们这儿是自增整数主键，
 *     打开表单时还没有 id，而 blob: URL 一刷新就废、跨页也传不过去。
 *  2. **贴链接导入是"提议"不是"入库"**：抓取结果填进同一个表单，用户核对改完
 *     再点保存才进库。自动抽取必然会有错（抽到推荐位文案、类目判断错），
 *     没有这道人工闸门，商品库里很快会堆一批错数据。
 *  3. **空状态给"导入示例商品"**：新用户第一次进来是空库，没有示例就只能自己
 *     找商品图，转化路径太长。
 */
export default function ProductsPage() {
  const nav = useNavigate();
  const { message, modal } = AntApp.useApp();

  const [products, setProducts] = useState<Json[]>([]);
  const [loading, setLoading] = useState(true);

  // 表单
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [name, setName] = useState('');
  const [category, setCategory] = useState('other');
  const [description, setDescription] = useState('');
  const [price, setPrice] = useState('');
  const [audience, setAudience] = useState('');
  const [images, setImages] = useState<DraftImage[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // 贴链接导入
  const [importOpen, setImportOpen] = useState(false);
  const [importUrl, setImportUrl] = useState('');
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importedNotice, setImportedNotice] = useState(false);

  /** 本次表单会话的图片归属 id。来源不同但语义一致：
   *  手工新建/贴链接导入时生成新的，编辑时沿用原商品 id（图片还能落在同一目录）。 */
  const draftIdRef = useRef<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setProducts(await api.listProducts());
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => { void load(); }, [load]);

  const resetForm = () => {
    setName(''); setCategory('other'); setDescription('');
    setPrice(''); setAudience(''); setSaveError(null);
    // 撤销预览用的 blob URL，否则连续开关表单会攒一堆内存
    setImages((prev) => {
      prev.forEach((i) => { if (i.file) URL.revokeObjectURL(i.url); });
      return [];
    });
    setFormOpen(false);
    setEditingId(null);
    draftIdRef.current = null;
  };

  const openCreate = () => {
    resetForm();
    draftIdRef.current = uid();
    setFormOpen(true);
  };

  // ---------------- 图片选择 / 拖拽 ----------------

  const addFiles = useCallback((files: FileList | null) => {
    if (!files) return;
    setImages((prev) => {
      const room = MAX_IMAGES - prev.length;
      if (room <= 0) return prev;
      const picked = Array.from(files)
        .slice(0, room)
        .filter((f) => f.type.startsWith('image/'))
        .map((file) => ({ id: uid(), url: URL.createObjectURL(file), file }));
      if (!picked.length) return prev;
      return [...prev, ...picked];
    });
  }, []);

  const removeImage = (id: string) => {
    setImages((prev) => {
      const hit = prev.find((i) => i.id === id);
      if (hit?.file) URL.revokeObjectURL(hit.url);
      return prev.filter((i) => i.id !== id);
    });
  };

  // ---------------- 保存 ----------------

  const save = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    setSaveError(null);
    try {
      const owner = editingId != null ? String(editingId) : (draftIdRef.current || uid());
      const fresh = images.filter((i) => i.file);
      let uploaded: string[] = [];
      if (fresh.length) {
        const r = await api.uploadImages('products', owner, fresh.map((i) => i.file!));
        uploaded = r.urls;
      }
      // 按原顺序拼最终 URL：新图用上传结果，老图保持原 URL
      let cursor = 0;
      const finalImages = images.map((i) => (i.file ? uploaded[cursor++] : i.url));

      const body: Json = {
        name: name.trim(), category, description: description.trim(),
        images: finalImages,
        price: price.trim(), target_audience: audience.trim(),
      };
      // R-27 手工标注优先：动过任意下拉才整组显式传 image_meta（未标注的图按
      // hero 兜底，与后端归一默认一致）；一张都没动 → 不传，保持 R-20 纯 AI
      // 识别，也不把已缓存的 AI 画面描述抹掉（image_meta 归一只留 role）
      if (images.some((i) => i.roleTouched)) {
        body.image_meta = images.map((i) => ({ role: normRole(i.role) }));
      }
      if (editingId != null) await api.updateProduct(editingId, body);
      else await api.createProduct(body);

      if (!editingId && draftIdRef.current) setImportedNotice(true);
      message.success(editingId != null ? '已保存修改' : '已加入商品库');
      resetForm();
      void load();
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const startEdit = (p: Json) => {
    setEditingId(p.id);
    setName(p.name || '');
    setCategory(p.category || 'other');
    setDescription(p.description || '');
    setPrice(p.price || '');
    setAudience(p.target_audience || '');
    setImages((p.images || []).map((url: string, i: number) => ({
      id: uid(), url,
      role: normRole(p.image_meta?.[i]?.role),
    })));
    setSaveError(null);
    setFormOpen(true);
  };

  const remove = (p: Json) => {
    modal.confirm({
      title: `删除商品「${p.name}」？`,
      content: '只删记录，磁盘上的商品图保留。',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await api.deleteProduct(p.id);
          if (editingId === p.id) resetForm();
          message.success('已删除');
          void load();
        } catch (e) { message.error((e as Error).message); }
      },
    });
  };

  // ---------------- 贴链接导入 ----------------

  const runImport = async () => {
    const url = importUrl.trim();
    if (!url || importing) return;
    setImporting(true);
    setImportError(null);
    try {
      // draftId 先生成：后端要按它把抓到的商品图落盘，并把 URL 直接回给我们
      const draftId = uid();
      const r = await api.importProductLink(url, draftId);
      draftIdRef.current = draftId;
      setEditingId(null);
      const parsed = r.product || {};
      setName(parsed.title || '');
      // 类目**不猜** —— 自动分类错得比标题还多，一律回落"其他"让用户自己选
      setCategory('other');
      setDescription(parsed.description || '');
      setPrice(parsed.price_text || '');
      setAudience('');
      setImages((r.images || []).map((u: string) => ({ id: uid(), url: u })));
      setSaveError(null);
      setFormOpen(true);
      setImportOpen(false);
      setImportUrl('');
    } catch (e) {
      setImportError((e as Error).message);
    } finally {
      setImporting(false);
    }
  };

  // ---------------- 示例商品 ----------------

  const importExamples = async () => {
    try {
      const list = await api.exampleProducts();
      const existing = new Set(products.map((p) => p.name));
      let added = 0;
      for (const ex of list) {
        if (existing.has(ex.name)) continue;
        await api.createProduct({
          name: ex.name, category: ex.category, description: ex.description,
          images: ex.images, price: ex.price,
          target_audience: ex.target_audience,
        });
        added += 1;
      }
      message.success(added ? `已导入 ${added} 个示例商品` : '示例商品已全部在库中');
      void load();
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const full = images.length >= MAX_IMAGES;

  return (
    <div className="cf-grid-bg" style={{ minHeight: '100%', padding: '28px 24px 48px' }}>
      <div style={{ maxWidth: 1180, margin: '0 auto' }}>
        {/* 页头 */}
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, marginBottom: 24 }}>
          <div>
            <h1 className="cf-page-title">
              <span className="cf-brand-text">商品库</span>管理
            </h1>
            <p className="cf-page-sub">集中管理你的商品信息，创建项目时可快速选用</p>
          </div>
          {!formOpen && (
            <Space>
              <Button icon={<LinkOutlined />}
                onClick={() => { setImportOpen((v) => !v); setImportError(null); }}>
                贴链接导入
              </Button>
              <Button className="cf-brand-gradient" icon={<PlusOutlined />} onClick={openCreate}>
                添加商品
              </Button>
            </Space>
          )}
        </div>

        {/* 入库成功后的快捷去向 */}
        {importedNotice && !formOpen && (
          <div className="cf-card" style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            gap: 12, padding: '10px 16px', marginBottom: 20,
            borderColor: 'rgba(82,196,26,0.35)', background: 'rgba(82,196,26,0.08)',
          }}>
            <span style={{ color: '#95de64', fontSize: 13 }}>✓ 已入库，可以直接批量出片了</span>
            <Space>
              <Button size="small" onClick={() => nav('/batch')}>去批量出片 →</Button>
              <Button size="small" type="text" icon={<CloseOutlined />}
                onClick={() => setImportedNotice(false)} />
            </Space>
          </div>
        )}

        {/* 贴链接导入 */}
        {importOpen && !formOpen && (
          <div className="cf-card cf-card-accent" style={{ padding: 20, marginBottom: 24 }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 12 }}>
              粘贴商品链接，自动提取信息入库
            </div>
            <Space.Compact style={{ width: '100%' }}>
              <Input
                value={importUrl}
                onChange={(e) => setImportUrl(e.target.value)}
                placeholder="https://…（商品详情页链接）"
                onPressEnter={runImport}
                allowClear
              />
              <Button className="cf-brand-gradient" disabled={!importUrl.trim() || importing}
                onClick={runImport}>
                {importing
                  ? <><LoadingOutlined /> 抓取解析中…</>
                  : '提取'}
              </Button>
            </Space.Compact>
            {importError && (
              <div style={{ marginTop: 10, fontSize: 13, color: '#ff7875' }}>
                <ExclamationCircleOutlined /> {importError}
                <Button type="link" size="small" style={{ padding: '0 6px' }}
                  onClick={() => {
                    setImportOpen(false);
                    resetForm();
                    draftIdRef.current = uid();
                    setFormOpen(true);
                  }}>
                  转手动填写 →
                </Button>
              </div>
            )}
            <p className="cf-hint" style={{ marginTop: 10 }}>
              自动抓取商品页并提取名称 / 卖点 / 价格 / 图片，提取结果全部可改，
              确认保存后才会进商品库。
            </p>
          </div>
        )}

        {/* 新增 / 编辑表单 */}
        {formOpen && (
          <div className="cf-card cf-card-accent" style={{ padding: 20, marginBottom: 24 }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 16 }}>
              {editingId != null ? '编辑商品' : '添加商品'}
            </div>

            {editingId == null && draftIdRef.current && (
              <div style={{
                marginBottom: 16, padding: '8px 12px', borderRadius: 8, fontSize: 12,
                border: '1px solid rgba(192,139,82,0.35)', background: 'rgba(192,139,82,0.10)',
                color: '#e0ab6d',
              }}>
                以下内容由链接自动提取，可能有误——请核对修改（尤其是类目），点「添加商品」确认入库
              </div>
            )}

            {/* 名称 */}
            <div style={{ marginBottom: 16 }}>
              <label style={labelStyle}>商品名称 <span style={{ color: '#ff7875' }}>*</span></label>
              <Input value={name} onChange={(e) => setName(e.target.value)}
                placeholder="例如：小米手环 8 NFC 版" />
            </div>

            {/* 品类 */}
            <div style={{ marginBottom: 16 }}>
              <label style={labelStyle}>商品品类</label>
              <Select value={category} onChange={setCategory} style={{ width: '100%' }}
                placeholder="选择商品品类" options={CATEGORY_OPTIONS} />
            </div>

            {/* 卖点描述 */}
            <div style={{ marginBottom: 16 }}>
              <div style={labelRowStyle}>
                <label style={labelStyle}>卖点描述</label>
                <span style={optionalStyle}>选填</span>
              </div>
              <Input.TextArea value={description} onChange={(e) => setDescription(e.target.value)}
                rows={3} placeholder="描述商品的核心卖点、独特优势…（会作为商品事实写进提示词）" />
            </div>

            {/* 图片 */}
            <div style={{ marginBottom: 16 }}>
              <div style={labelRowStyle}>
                <label style={labelStyle}>商品图片</label>
                <span style={optionalStyle}>{images.length}/{MAX_IMAGES} 张</span>
              </div>

              {!full && (
                <div
                  className={`cf-drop${dragging ? ' is-over' : ''}`}
                  onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                  onDragLeave={(e) => { e.preventDefault(); setDragging(false); }}
                  onDrop={(e) => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files); }}
                  onClick={() => fileRef.current?.click()}
                >
                  <input ref={fileRef} type="file" accept="image/*" multiple hidden
                    onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }} />
                  <div className="cf-drop-icon"><InboxOutlined /></div>
                  <div style={{ fontSize: 13 }}>
                    拖拽图片到这里，或 <span className="cf-brand-text" style={{ fontWeight: 600 }}>点击上传</span>
                  </div>
                  <p className="cf-hint" style={{ marginTop: 4 }}>
                    支持 JPG / PNG / WebP，最多 {MAX_IMAGES} 张
                  </p>
                </div>
              )}

              {images.length > 0 && (
                <>
                  <p className="cf-hint" style={{ marginTop: 8 }}>
                    图片用途默认由 AI 在<b>生成视频脚本时</b>自动识别（保存不再识别，避免提前花钱）；
                    <b>AI 判错了可直接在图下方改</b>——手工标注优先生效，AI 只补画面描述、
                    不会覆盖你定的用途。商品本体的清晰照（主体图）会作为参考图发给模型，
                    信息图/海报不会进画面。<b>不同型号/花色请拆成独立商品</b>（一物一档），
                    否则会被一起画进视频
                  </p>
                  <div className="cf-img-grid" style={{ marginTop: 8 }}>
                    {images.map((img) => (
                      <div key={img.id} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <div className="cf-img-cell">
                          <img src={img.url} alt="商品图片" />
                          <button className="cf-img-del" title="移除"
                            onClick={() => removeImage(img.id)}>
                            <CloseOutlined />
                          </button>
                        </div>
                        <Select
                          size="small"
                          style={{ width: '100%' }}
                          value={img.role || undefined}
                          placeholder="AI 自动识别"
                          options={ROLE_OPTIONS}
                          onChange={(v) => setImages((prev) => prev.map((it) => (
                            it.id === img.id ? { ...it, role: v, roleTouched: true } : it
                          )))}
                        />
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>

            {/* 价格 / 目标人群 */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16 }}>
              <div>
                <div style={labelRowStyle}>
                  <label style={labelStyle}>价格信息</label>
                  <span style={optionalStyle}>选填</span>
                </div>
                <Input value={price} onChange={(e) => setPrice(e.target.value)} placeholder="例如：¥199" />
              </div>
              <div>
                <div style={labelRowStyle}>
                  <label style={labelStyle}>目标人群</label>
                  <span style={optionalStyle}>选填</span>
                </div>
                <Input value={audience} onChange={(e) => setAudience(e.target.value)}
                  placeholder="例如：18-35 岁女性" />
              </div>
            </div>

            {saveError && (
              <div style={{ marginBottom: 12, fontSize: 13, color: '#ff7875' }}>
                <ExclamationCircleOutlined /> {saveError}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <Button onClick={resetForm} disabled={busy}>取消</Button>
              <Button className="cf-brand-gradient" loading={busy}
                disabled={!name.trim()} onClick={save}>
                {editingId != null ? '保存修改' : '添加商品'}
              </Button>
            </div>
          </div>
        )}

        {/* 空状态 / 列表 */}
        {!loading && products.length === 0 && !formOpen ? (
          <div className="cf-card">
            <div className="cf-empty">
              <div className="cf-empty-icon"><PictureOutlined /></div>
              <p style={{ color: 'var(--cf-text-dim)', marginBottom: 16 }}>
                还没有商品，添加你的第一个商品
              </p>
              <Space>
                <Button className="cf-brand-gradient" icon={<PlusOutlined />} onClick={openCreate}>
                  添加商品
                </Button>
                <Button onClick={importExamples}>导入示例商品</Button>
              </Space>
              <p className="cf-hint" style={{ marginTop: 12 }}>
                没有现成商品？先导入 3 个示例商品试试批量出片
              </p>
            </div>
          </div>
        ) : products.length > 0 && (
          <>
            <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 16 }}>
              <span style={{ fontSize: 15, fontWeight: 600 }}>全部商品</span>
              <span style={{ fontSize: 13, color: 'var(--cf-text-dim)' }}>{products.length} 个商品</span>
            </div>

            <div style={{
              display: 'grid', gap: 16,
              gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))',
            }}>
              {products.map((p) => {
                const meta = categoryMeta(p.category);
                return (
                  <div className="cf-card cf-card-hover" key={p.id}>
                    <div className="cf-thumb">
                      {p.cover
                        ? <img src={p.cover} alt={p.name} />
                        : <div className="cf-thumb-empty"><PictureOutlined /></div>}
                      <span style={{
                        position: 'absolute', top: 8, left: 8, padding: '1px 8px',
                        borderRadius: 10, fontSize: 11, lineHeight: '17px',
                        background: 'rgba(0,0,0,0.55)', color: meta.color,
                        border: `1px solid ${meta.color}55`, backdropFilter: 'blur(4px)',
                      }}>{meta.label}</span>
                      <div className="cf-thumb-actions">
                        <button className="cf-icon-btn" title="编辑" onClick={() => startEdit(p)}>
                          <EditOutlined />
                        </button>
                        <button className="cf-icon-btn danger" title="删除" onClick={() => remove(p)}>
                          <DeleteOutlined />
                        </button>
                      </div>
                    </div>
                    <div style={{ padding: 14 }}>
                      <div style={{
                        fontSize: 13, fontWeight: 500, whiteSpace: 'nowrap',
                        overflow: 'hidden', textOverflow: 'ellipsis',
                      }} title={p.name}>{p.name}</div>
                      <div style={{ display: 'flex', alignItems: 'center', marginTop: 6, minHeight: 20 }}>
                        {p.price && (
                          <span style={{ fontSize: 12, color: '#e0ab6d', fontWeight: 500 }}>{p.price}</span>
                        )}
                        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--cf-text-faint)' }}>
                          {p.code}
                        </span>
                      </div>
                      <Button className="cf-brand-gradient" size="small" block
                        style={{ marginTop: 10 }}
                        icon={<VideoCameraOutlined />}
                        onClick={() => nav(`/?productId=${p.id}`)}>
                        做视频
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}

        {loading && (
          <Typography.Text type="secondary"><LoadingOutlined /> 加载中…</Typography.Text>
        )}
      </div>
    </div>
  );
}

const labelStyle: React.CSSProperties = { fontSize: 13, fontWeight: 500, display: 'block', marginBottom: 6 };
const labelRowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 6,
};
const optionalStyle: React.CSSProperties = { fontSize: 11, color: 'var(--cf-text-faint)' };
