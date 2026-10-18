# BUG 报告：EXIF 写入破坏实况照片（Live Photo）

> 状态：**已定位并实证** · 仅分析，未改代码
> 基线：`3b53f0f`（tests: 157 passed）
> 样本来源：`D:\dev-apps\live-repair\fixtures`、`verify_samples`

---

## 1. 结论（TL;DR）

**根因**：写入走的是「解码 → 重新编码 JPEG → 新文件」，而不是「就地修改 EXIF 字段」。

`apply_exif_fields()` 最后一行是 `img.save(output_path, exif=exif_bytes)`。Pillow 保存 JPEG 时会**重新编码像素并只写它认识的段**，由此产生三个致命后果：

| # | 后果 | 影响 |
|---|---|---|
| 1 | **丢弃 EOI 之后的全部字节** | 实况照片的视频轨（追加在 JPEG 尾部）被整体删除 → 变静态图，**不可逆** |
| 2 | **丢弃 XMP 段（APP1）** | `GCamera:MotionPhoto=1`、`Container:Item` 等实况声明消失 |
| 3 | 像素重新编码 | 无谓的二次有损压缩（quality=100 也不是无损） |

实况照片的格式前提是「**同一文件 = JPEG 主图 + 尾部视频 + XMP 声明**」三者共存。当前实现把三者中的两者（视频、XMP）直接扔掉，只剩主图。

---

## 2. 实证数据

用生产函数 `apply_exif_fields()`（只改一个 `Make` 字段）跑真实样本，输出到临时目录：

| 样本 | 原始大小 | 写入后 | 损失 | `ftyp`（视频） | XMP | `MicroVideo`/`MotionPhoto` |
|---|---|---|---|---|---|---|
| `fixtures/anchor.jpg` | 8,604,119 | 8,050,375 | **−553,744（6.4%）** | ❌ 消失 | 无 | 无 |
| `fixtures/expected/retain/LivePhoto_20260506_013840_350.jpg` | 7,100,844 | 4,609,965 | **−2,490,879（35.1%）** | ❌ 消失 | ❌ 消失 | ❌ 消失 |
| 合成样本（JPEG + XMP + 伪 MP4） | 305,975 | 5,491 | **−98.2%** | ❌ 消失 | ❌ 消失 | ❌ 消失 |

样本结构（`LivePhoto_..._350.jpg`，按 JPEG 段扫描）：

```
E0(JFIF) 22B | E1(Exif) 21,326B | E1(XMP) 1,071B | E2(ICC)×2 | EA | DQT | DRI | SOF0 | DHT
                                       ↑ APP1 XMP：GCamera:MotionPhoto=1 + Container:Item
SOS 熵数据 2,230,950B → EOI → 视频 4,749,918B（ftyp 'qt  ' + box 链）
```

写入后：`ftyp` 与 XMP 段全部消失，文件只剩 JPEG 主图。

---

## 3. 为什么「用 piexif.insert」仍然不够

`piexif.insert()` 能保住视频和 XMP（实测：文件 7,100,844 → 7,080,668，视频区完整），但它内部是：

```
piexif.load(原 EXIF) → 改字段 → piexif.dump(整个 TIFF) → 替换 APP1 段
```

**`dump()` 会重建整个 TIFF 结构**，实测后果：

| 项 | 原始 | insert 后 |
|---|---|---|
| EXIF 段长度 | 21,326 B | **1,172 B** |
| APP0 (JFIF) 段 | 有 | **丢失** |
| IFD 标签数量 | IFD0 12 / Exif 35 / GPS 15 | 相同 |
| 未被任何 IFD 引用的字节 | **20,906 B** | 0 |

那 20,906 B 是**孤儿数据**：该样本是「互传破坏过」的文件，MakerNote 的 `0x927C` tag 已被摘掉，但原始字节残留在段内（可在其中读到 `iPhone 14 Pro`、`2026:05:05 15:55:31`）。`dump()` 不保留未引用区域，于是段从 21KB 缩到 1.2KB。

结论：`piexif.insert` 保住了「文件级」的东西（视频、XMP），但仍在「EXIF 段级」重建了结构，并且丢掉了 APP0。这与「**只改原文件里对应的那几个字段**」的要求不符。

---

## 4. 偏移量机制核查（用户特别关注）

实况照片文件里有 **三套独立的偏移/长度声明**，全部实测确认：

### 4.1 XMP `Item:Length` — 从文件末尾倒数

样本 `LivePhoto_..._350.jpg`：

```
文件总长        7,100,844
Item:Length     4,749,918   ← MotionPhoto 项的视频字节数
视频起点        7,100,844 − 4,749,918 = 2,350,926
实测 ftyp 位置  2,350,926    ✓ 完全吻合
```

语义是「**长度**」而非「偏移」，起点由 `文件总长 − 长度` 推出。
→ **EXIF 段变长 N 字节后，文件总长 +N、视频绝对位置也 +N，差值不变，自动正确。**

### 4.2 MPF（APP2）`MP Entry` — 相对 MPF 头，同步平移

该样本的 APP2 里有一个 `MPF\0` 段（3 个 tag：0xB000 / 0xB001 / 0xB002），`MP Entry` 有两个条目：

| 条目 | size | rel offset | 绝对位置 | 那里是什么 |
|---|---|---|---|---|
| entry[0]（主图） | 2,256,089 | 0 | 22,429 | MPF 自己的 TIFF 头 |
| entry[1]（第二图） | 95,564 | 2,232,933 | 2,255,362 | **`FFD8 FFE1` = 另一个 JPEG 的 SOI** |

**发现了一个之前没注意的东西**：主图 EOI（2,255,362）和视频起点（2,350,926）之间，夹着一张 **95 KB 的隐藏 JPEG**——很可能是 HDR 增益图（gain map）。当前 `img.save()` 会把它连同视频一起丢掉。

关键验证：把 EXIF 段强行缩短 20,153 字节后重新解析 MPF——

```
insert 前: MPF 段在 22,421，entry[1].abs = 2,255,362，head = FFD8 FFE1  ✓
insert 后: MPF 段在  2,268，entry[1].rel 仍是 2,232,933
          entry[1].abs = 2,235,209，head = FFD8 FFE1  ✓ 依然正确
```

→ **MPF 的 offset 是「相对 MPF 头」的，而 MPF 段与它指向的数据都在 EXIF 段之后，EXIF 长度变化时两者同步平移，相对关系不变。**

### 4.3 MP4 内部 `stco` — 相对视频块

```
top-level boxes: ftyp(20) wide(8) mdat(4,743,749) moov(6,081)
stco 条目 6 个: 196,090 / 1,242,043 / … / 4,634,015
视频块长度     4,749,918
max(stco) 4,634,015 < 4,749,918   ← 全部落在视频块内部
```

→ chunk offset 是**相对视频块起点**的，视频整块平移不影响。

### 4.4 结论

| 机制 | 语义 | EXIF 段长度变化时 |
|---|---|---|
| XMP `Item:Length` | 从末尾倒数的长度 | ✅ 自动正确 |
| MPF `MP Entry` offset | 相对 MPF 头 | ✅ 自动正确（同步平移） |
| MP4 `stco` | 相对视频块 | ✅ 自动正确 |
| MPF `entry[0].size` | 主图字节数 | ⚠️ **不会自动更新**（见下） |

**唯一不会自动跟随的是 MPF `entry[0].size`**（主图长度）。实测该值本就与主图实际 EOI 位置差 727 字节，说明厂商实现并不严格；且图像定位只依赖 `entry[1].offset`（相对值，安全），因此不影响可读性。但整改方案里仍会：

- 优先让 EXIF 段**长度保持不变**（定长字段原地覆盖、ASCII 变短补零），从根上避免位移；
- 必须变长时（Make/Model 写更长的值），**同步把 `entry[0].size` 加上相同的增量**，并在写入后自检。

---

## 5. 受影响场景（按严重度）

| 场景 | 条件 | 后果 |
|---|---|---|
| **S1 实况照片（Android/Google Motion Photo）** | 文件尾部追加了 MP4（vivo、荣耀、小米、三星、部分 OPPO） | 视频轨被删除，相册里变静态图，**原始视频不可恢复** |
| **S1b HDR 增益图丢失** | 主图与视频之间夹了一张隐藏 JPEG（本例 95 KB） | 高动态范围信息丢失（当前实现一并删除） |
| **S2 任何带 XMP 的照片** | 相机/手机写入 XMP（HDR 增益图声明、Motion Photo、评级、部分水印） | XMP 段丢失，相关特性失效 |
| **S3 覆盖模式（Overwrite）** | 用户勾选原地覆盖 | 备份存在时可 Rollback；**退出时若选「否·保留修改并删除备份」，损坏即永久** |
| **S4 输出副本模式** | 默认 `_exif` 副本 | 原文件安全，但产出的副本是坏图（视频缺失），用户可能直接拿它替换原件 |
| S5 iPhone Live Photo（JPEG 主图 + 外挂 MOV） | `.heic` 不在支持列表，但 `.jpg` 主图会中招 | 主图资产标识可能受影响；MOV 不受影响 |

不受影响：纯静态 JPEG / PNG / TIFF（没有追加数据，也没有 XMP 的）。

---

## 5. 整改方案

### 5.1 原则（按用户明确指示）

> 「修改原文件里的 EXIF 对应字段，不是用新的 EXIF 造一个新文件」

即：**除目标字段所在的字节外，文件其余部分逐字节不变**。视频、XMP、ICC、APP0、像素数据一律不动。

### 5.2 方案对比

| 方案 | 视频/XMP | EXIF 段其他数据 | 像素 | 结论 |
|---|---|---|---|---|
| 现状 `img.save()` | ❌ 丢 | ❌ 重建 | ❌ 重编码 | 必须废弃 |
| `piexif.insert` | ✅ 保留 | ❌ 重建（实测 21KB→1.2KB，丢 APP0） | ✅ 不动 | 仅作降级路径 |
| **字节级 patch（采用）** | ✅ 保留 | ✅ 不动 | ✅ 不动 | 满足要求 |

### 5.3 技术设计

新增一个纯函数模块（建议 `exif_patch.py`，可独立单测），输入原文件字节 + 字段字典，输出新字节：

```
patch_exif_bytes(raw: bytes, fields: dict) -> bytes
  ├─ scan_segments(raw)        定位 SOI / APPn / SOS / EOI（借 live-repair M1 的段扫描思路）
  ├─ parse_tiff(app1_payload)  解析 byte order、IFD0、SubIFD(0x8769)、GPSIFD(0x8825)
  ├─ for each field:
  │    ├─ 定长类型（RATIONAL 8B / SHORT 4B / ASCII 20B 日期）
  │    │     → 原地覆盖 value 字节（或 offset 指向的 data 区），**段长不变**
  │    ├─ ASCII 变短（Make/Model）
  │    │     → 改 count + 余下补 0x00，**段长不变**
  │    └─ ASCII 变长
  │          → 新字符串追加到 TIFF 末尾（4 字节对齐），只更新该 tag 的 offset/count
  │          → **同时把 MPF 的 entry[0].size 加上相同的增量**（见 4.4）
  └─ 需要「新增 tag」或「删除整个 GPS」
       → 降级：重建 EXIF 段（piexif.dump），但用**自己的段拼接**写回，
         保证 APP0/XMP/MPF/ICC/EA/像素/尾部视频逐个原样复制，
         并按段长变化量修正 MPF entry[0].size
```

**偏移量处理策略**（针对 §4 的核查结果）：

1. 首选「段长不变」路径：定长字段与 ASCII 变短都能做到零位移，三套偏移天然不受影响；
2. 段长必须变化时，只改 `MPF entry[0].size`（`+= delta`），**不碰 entry[1].offset**（相对值，同步平移后仍正确）；
3. 写入后强制自检（失败则回退到降级路径或报错，绝不留坏文件）：
   - `raw.find(b"ftyp") - 4 == len(raw) - declared_video_length`（XMP `Item:Length` / 旧式 `MicroVideoOffset`）
   - `MPF entry[1]` 指向的位置必须以 `FFD8`（JPEG SOI）开头
   - XMP 段仍然存在且长度未变
   - 主图 EOI 位置 = 源 EOI + delta

关键约束（写进测试）：

1. `output[0 : exif_data_start]` 与源**逐字节相同**（APP0/XMP 都在这之前或之后原样保留）
2. `output[eoi:]`（视频区）与源**逐字节相同**
3. 源文件大小变化 = 仅 EXIF 段长度的净变化
4. 目标字段的新值可被 `read_exif_info()` 正确读出
5. 幂等：对已写入的文件再写一次相同值，字节不再变化
6. **偏移自洽**：`ftyp` 位置、MPF `entry[1]` 目标位置、`stco` 全部落在正确区间

### 5.4 改动范围

| 文件 / 函数 | 改动 |
|---|---|
| `exif_patch.py`（新增） | JPEG 段扫描 + TIFF 解析 + 三种写入策略（约 250–300 行） |
| `apply_exif_fields()` | 不再接收 `img`，改为接收源字节 → 返回新字节；PNG/TIFF 保留 Pillow 路径 |
| `_write_worker()` | 保存前读原字节 → patch → 写目标；覆盖模式仍走 `bm.safe_write` 原子写 |
| `BackupManager.safe_write` | 接口不变（回调返回字节或写文件），必要时适配 |
| `tests/test_motion_photo.py`（新增） | 见 5.5 |
| `README` | 说明实况照片现在会被正确保留 |

**不做**：HEIC 支持、视频重编码、XMP 内容改写（只保证不丢）。

### 5.5 验证方式

**A. 单元测试（合成样本，快）**

1. 造「JPEG + XMP(MotionPhoto=1) + 尾部伪 MP4」，改 `Make` →
   - 断言 `ftyp` 仍在、尾部 300KB 逐字节相同、XMP 段完整
2. 改 `DateTimeOriginal`（20 字节定长）→ 断言 EXIF 段长度不变、其他字节不变
3. 改 GPS 三个字段 → 断言 RATIONAL 区被覆盖、GPS IFD 结构不变
4. `Make` 从短变长/从长变短 → 断言 count/offset 正确，其余 tag 字节不变
5. 「新增 GPS」（原文件无 GPS IFD）→ 走降级路径，断言视频/XMP 仍完整
6. 幂等性

**B. 真实样本回归（关键）**

对 `live-repair` 的 3 个样本逐个：

| 断言 | 方法 |
|---|---|
| 视频区逐字节相同 | `out[ftyp-4 : ] == src[ftyp-4 : ]`（ftyp 偏移按各自文件重新定位） |
| XMP 段完整 | 输出包含 `ns.adobe.com/xap` 且段长与源一致 |
| **XMP 声明自洽** | `len(out) - Item:Length == ftyp 位置` |
| **MPF 偏移自洽** | `MPF entry[1]` 目标处必须以 `FFD8` 开头；`entry[0].size` 与主图 EOI 差值与源一致 |
| **隐藏图完整** | 主图 EOI 到视频起点之间的字节（本例 95 KB）逐字节相同 |
| **MP4 内部偏移** | `max(stco) < 视频块长度`（平移后仍成立） |
| EXIF 段其他 tag | `piexif.load` 逐 tag 对比，除目标字段外全部相等 |
| 文件大小差 | 等于 EXIF 段长度差（不是少几十万字节） |
| 字段生效 | `read_exif_info()` 读回新值 |

**C. 端到端**

- 用程序 UI 选目录 → 勾选 → 写入 → 用 `live-repair` 的分析逻辑确认 `need_fix` 状态没有变差
- 覆盖模式写入 → Rollback → 文件 SHA256 回到原始值

**D. 防回归**

把「写入后视频区与 XMP 必须与源一致」做成一条硬性断言测试（类似现有的 zero-IO 护栏测试），任何未来改动若重新引入 `img.save()` 会立刻失败。

---

## 6. 风险与待确认

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 变长 ASCII 追加导致 EXIF 段变长，理论上影响 `MicroVideoOffset` | 该 offset 是「从文件末尾倒数」，段变长不影响；但仍需真实样本验证 |
| R2 | 部分厂商 EXIF 结构不规范（多 IFD 链、私有子 IFD） | 解析失败时降级为「重建 EXIF 段 + 原样拼接其他段」，绝不用 `img.save()` |
| R3 | TIFF 解析手写易错（偏移/对齐） | 独立模块 + 对 3 个真实样本做逐字节断言 |
| R4 | 用户已有损坏文件 | 备份仍在时可 Rollback；一旦删备份则不可恢复（视频已物理删除） |

**待确认**：现有已损坏的文件是否需要补救？——如果源盘（NAS）还有原件，重新跑一次即可；如果只有 ExifCraft 覆盖后的文件，则视频已丢失，无法从该文件恢复。

---

## 7. 建议实施顺序

1. `exif_patch.py` + 单测（不接 UI，可独立验证）
2. 接入 `apply_exif_fields()`，保留 PNG/TIFF 旧路径
3. 真实样本回归（B 组断言）
4. `_write_worker` 接线 + 端到端
5. 打包 + 在真实库上灰度验证
