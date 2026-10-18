# 三段式布局 + 可拖拽分栏 · 技术优化方案

> 状态：待确认 · 基线 commit `5fbcf35`（tests: 135 passed）
> 目标：文件 / EXIF+日志 / 预览 三列独立，列宽与中间列内部高度均可拖拽

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 布局技术 | 外层 `ttk.Panedwindow(horizontal)` 三列；中间列内嵌 `ttk.Panedwindow(vertical)` 分「表单 / 日志」 |
| 拖拽 | 原生 sash，3 处可拖（2 条竖 + 1 条横），无需自绘拖拽逻辑 |
| 日志 | 从窗口底部整行移到中间列底部，`Text height=4`，随中间列一起拖拽 |
| 文件列 | 初始宽度 ~330px（够 36+245+62），不再固定 50% |
| 风险等级 | **中偏高**——动的是整个 body 布局，但每个 pane 内容是原样搬迁，不改业务逻辑 |
| 改动面 | `_build()` 的 body 区约 +120/−60 行；路径控件改造 +70 行；滚轮路由 +20 行；预览联动 +25 行；日志迁移 +25 行 |

---

## 1. 现状分析

### 1.1 当前结构

```
outer (padding=12)
├── header                      (pack top)
├── body                        (pack fill=both expand)   ← grid 两列
│   ├── column 0  (uniform="col") 50%
│   │   └── src_card: Path / Output / 文件表格 / footer / Overwrite
│   └── column 1  (uniform="col") 50%
│       └── canvas(滚动) → 快捷选择 / Preview / Camera / Exposure / Date / GPS
├── actions                     (pack bottom)
├── prog_row                    (pack bottom)
├── log_holder                  (pack bottom, fill=x)     ← 整行宽、仅 4 行
└── status_bar                  (pack bottom)
```

### 1.2 问题（量化）

| # | 问题 | 数据 |
|---|---|---|
| P1 | 左右各占 50%（`uniform="col"`），文件表格实际只需要 ~350px | 1920 宽窗口下左侧白占 ~600px |
| P2 | EXIF 表单和预览挤在同一列，看预览就得滚动，滚动后就看不到字段 | 右列自然高 ~1050px，视口 ~700px |
| P3 | 日志横跨整个窗口宽度，只显示 4 行 | 1800px 宽 × 4 行，信息密度极低 |
| P4 | 预览宽度被 `uniform` 锁死在 50%，不能按需给图片更多空间 | 拖拽调整只能靠改窗口大小 |

### 1.3 保留不动

- 底部 `actions / prog_row / status_bar` 的 **bottom-up pack 顺序**（历史上修过的 bug：body 必须最后 pack，否则矮窗口会把按钮挤没）
- 文件表格的「文件名列自适应」逻辑（`_fit_name_column` 读的是 `winfo_width()`，pane 变宽会自动跟随）
- 预览盒的拖拽手柄（改高度）与解码预算（读实时控件尺寸）
- 所有业务方法（写入、备份、回滚、读 EXIF）

---

## 2. 目标结构

```
outer (padding=12)
├── header
├── body ── main_paned : ttk.Panedwindow(horizontal)
│   ├── [pane 1] 文件模块        weight=2   初始 ~330
│   │   └── src_card → Path / Output / 表格 / footer / Overwrite
│   ├── [pane 2] mid_paned : ttk.Panedwindow(vertical)   weight=3
│   │   ├── [pane 2a] 快捷选择 + 表单(canvas 滚动)      weight=4
│   │   └── [pane 2b] 日志 (Text height=4)              weight=1
│   └── [pane 3] 预览模块        weight=3
│       └── prev_card → 图片 / meta / 拖拽手柄
├── actions / prog_row / status_bar        （保持原样）
```

视觉：

```
┌──────────────────────────────────────────────────────────────┐
│ ExifCraft v2.1                                 Professional  │
├──────────────┬─────────────────────────┬─────────────────────┤
│ Source       │ 快捷选择 设备 曝光 时间 GPS 全部 │ Preview        │
│ Path [....]  │ ─────────────────────── │ ┌───────────────┐  │
│ Output [...] │ Camera · 相机           │ │   照片         │  │
│ ┌──────────┐ │  □ Make    □ Model      │ └───────────────┘  │
│ │ 文件 状态 │ │ Exposure · 曝光参数     │ ··· 拖拽手柄       │
│ │ ...      │ │  □ Focal □ Aperture ... │ IMG_0001 · 6 MB    │
│ └──────────┘ ├─────────────────────────┤                    │
│ 全选 全不选  │ Activity（4 行）         │                    │
│ □ Overwrite  │ ✔ Loaded EXIF ...        │                    │
├──────────────┴─────────────────────────┴─────────────────────┤
│ [Read EXIF] [Write EXIF] [CSV] [Rollback]     ▓▓▓░░ 进度     │
├──────────────────────────────────────────────────────────────┤
│ Ready                                      备份 12 个 / 3 MB │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. 技术选型

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **ttk.Panedwindow** | 原生拖拽、键盘可达、零自绘逻辑、Tk 内置 | sash 外观受主题限制，需要样式微调 | **采用** |
| 自绘 sash（Frame + B1-Motion） | 外观完全可控 | 要自己处理光标、最小尺寸、双击复位、键盘，易出 bug | 否 |
| grid + columnconfigure | 轻量 | 拖拽逻辑仍要自写 | 否 |

关键参数：

```python
main = ttk.Panedwindow(body, orient="horizontal")
main.add(files_pane, weight=2)
main.add(mid_paned,  weight=3)
main.add(prev_pane,  weight=3)

mid = ttk.Panedwindow(main, orient="vertical")
mid.add(form_pane, weight=4)
mid.add(log_pane,  weight=1)
```

- `sashwidth` 设为 `_px(7)`（默认 2–3px 太细）
- 初始 sash 位置：`update_idletasks()` 后 `main.sashpos(0, self._px(330))`，避免启动时按 weight 粗暴分配

---

## 4. 关键实现点

### 4.1 滚动路由（现有逻辑的迁移）

当前 `_wheel_over_right()` 判断指针是否落在右列 canvas 子树，然后 `bind_all("<MouseWheel>")` 接管。改后：

| 控件 | 滚轮行为 | 由谁处理 |
|---|---|---|
| 文件表格 | 滚列表 | Treeview 原生（`_on_mousewheel` 返回 `None`） |
| 中间表单 | 滚表单 | `_on_mousewheel` → `_scroll_form_column()` |
| 日志 | 滚日志 | Text 原生 |
| 预览 | 不滚（无溢出） | — |

→ `_wheel_over_right` 改名为 `_wheel_over_form`，判断对象换成表单 canvas，**逻辑不变**。

### 4.2 预览随列宽联动

拖拽竖 sash 会改变预览列宽 → 图片需要按新尺寸重解码（22ms/张）。

```python
prev_pane.bind("<Configure>", self._schedule_preview_refit)

def _schedule_preview_refit(self, _e=None):
    # 150ms 防抖：拖拽过程中会连续触发几十次 Configure
    ...
def _refit_preview(self):
    if self._preview_budget() == tuple(self.PREVIEW_MAX):
        return                      # 尺寸没变，别浪费一次解码
    self._resize_preview_budget()
```

现有的 `_resize_preview_budget()` 直接复用（它会 pop 缓存并重解码）。

### 4.3 日志迁移

- `log_holder` 从 `outer` 底部移到 `log_pane`
- 加 `Card` 包装（标题 "Activity · 日志"），与其他卡片视觉统一
- `Text height=4` 保持（4 行 ≈ 用户说的"几行"），新消息 `see("end")` 已有
- 日志 pane 可被拖到 1 行或拉高，Panedwindow 保证不塌陷（`weight` + 内容最小请求高度）

### 4.4 文件模块瘦身

- 表格不再吃 50%，起始 330px；`_fit_name_column` 会自动跟随 pane 宽度
- **Path / Output 不压缩**：路径本来就长，压成一行只会让尾部被截断
- 最小宽度保护：`files_pane` 里的表格 `minwidth` 由内容决定，Panedwindow 自会限制 sash 不越过内容最小尺寸

### 4.5 长路径：右对齐 + 尾部优先 + 可拖动查看前面

**实测结论**（`_probe_entry2.py`，107 字符路径 / 420px 宽）：

| 设置 | xview | 屏幕上的样子 |
|---|---|---|
| `justify="left"`（现状） | (0.00, 0.66) | 从盘符开始，尾部被裁 |
| `justify="right"` | (0.00, 0.66) | **和 left 完全一样** |
| `justify="right" + xview_moveto(1.0)` | (0.35, 1.00) | 尾部可见（文件名 + 上级目录） |

→ `justify="right"` **只在文本不溢出时**影响对齐；溢出后它不做任何滚动。必须显式 `xview_moveto(1.0)`。

**最终方案**（保持单行，不换行）：

```
Path    [ Entry（justify=right，尾部优先）.............. ]
        [ 细水平滚动条 ................................ ]
```

- 短路径：右对齐显示，滚动条停在原位
- 长路径：set 值后 `after_idle(xview_moveto(1.0))` → 直接看到尾部（文件名 + 几级上级目录）
- 想看开头：拖滚动条，或点进输入框按 Home
- 每次 File / Folder / Browse 设值后重新滚到尾部
- 滚动条常驻但只有 `_px(9)` 高——避免"溢出才出现"导致的整行高度跳动

实现：
```python
entry = ttk.Entry(row, textvariable=self.src_var, justify="right", ...)
hsb   = ttk.Scrollbar(row, orient="horizontal", command=entry.xview)
entry.configure(xscrollcommand=hsb.set)
self.src_var.trace_add("write", lambda *_: self._scroll_path_to_tail(entry))
```

`_scroll_path_to_tail` 用 `after_idle`，因为 StringVar 刚写入时 Entry 还没重新布局，立即 `xview_moveto` 会被忽略。

### 4.6 窗口尺寸

- `_setup_window()` 的 `need_w` 会变成三列之和，仍然 clamp 到 `sw * 0.8`，逻辑不变
- `minsize` 从 `1000x680` 提到 `1060x680`（三段式的最小可用宽度）

---

## 5. 分步实施

| 步骤 | 内容 | 验收 |
|---|---|---|
| **S1** | body 换成 horizontal Panedwindow，三个 pane 原样搬运现有内容 | 窗口能起、三列并排、功能不变、135 测试通过 |
| **S2** | 中间列改成 vertical Panedwindow；日志从底部移入 | 日志在中间列底部、4 行、可拖拽高度 |
| **S2b** | Path / Output 换成 2~4 行动态高度、按字符换行的只读 Text（双击可编辑） | 200+ 字符的路径完整可见、可选中复制、双击能改、失焦回只读 |
| **S3** | 滚轮路由迁移（`_wheel_over_form`） | 表单区滚轮正常；文件表格/日志滚轮不受影响 |
| **S4** | 预览随列宽重解码（防抖 + 尺寸比较） | 拖 sash 时图片不裁、不糊，松手后 ≤150ms 内刷新 |
| **S5** | sash 样式与初始位置（`sashwidth`、`sashpos`、颜色） | 分隔条看得见、好拖、初始比例合理 |
| **S6** | 测试 + 冒烟 + 打包 | 全量测试、三处拖拽冒烟、DPI 缩放核对、exe 启动 |

依赖：S1 → S2 → S3 顺序执行；S2b 可在 S1 后独立做；S4/S5 可在 S2 后并行；S6 最后。

---

## 6. 风险与缓解

| # | 风险 | 概率/影响 | 缓解 |
|---|---|---|---|
| R1 | 破坏 body 的 bottom-up pack 顺序，矮窗口下按钮被挤出 | 中/高 | 只替换 body **内部**结构，`body.pack(...)` 仍保持在最后一行 |
| R2 | Panedwindow 的 pane 不支持直接滚动 | 高/低 | 表单区继续用「canvas + inner frame」封装，沿用现有 `_sync_right_scroll` |
| R3 | 拖拽 sash 触发大量 `<Configure>`，预览反复解码卡顿 | 高/中 | 150ms 防抖 + 尺寸相同直接跳过 |
| R4 | 滚轮路由改名后漏改判断，导致列表滚轮失效或表单抢事件 | 中/中 | 冒烟覆盖 4 个区域（表格/表单/日志/预览）的滚轮行为 |
| R5 | sash 在 clam 主题下过细、几乎点不到 | 高/中 | `sashwidth=self._px(7)` + 自定义 `Sash` 元素颜色 |
| R6 | 文件列被拖得过窄，表格内容溢出 | 中/低 | 表格列宽有最小值（`_fit_name_column` 下限 80px），Panedwindow 会止于内容最小宽度 |
| R7 | 现有布局测试依赖 1100×800 窗口下的 bbox | 中/中 | `table_app` fixture 加宽到 1280，必要时 skip 而非失败 |
| R8 | DPI aware 下 sash / 少量像素值不缩放 | 中/中 | 所有手写像素走 `_px()` |
| R9 | 日志移出后 `_log()` 调用时机早于日志控件创建 | 低/高 | 日志控件仍在 `_build()` 内创建，只是换了父容器；`_log` 的调用都在 `_build` 之后 |
| R10 | 路径 Text 的 state 切换漏某条路径 → 点了没反应 / 一直可编辑 | 中/中 | 统一收口成 `_set_path_text()` / `_lock_path_text()` 两个方法，业务代码只调它们 |
| R11 | 超长路径动态算行数时 Text 尚未布局完，`count(displaylines)` 返回 0 | 中/低 | 用 `after_idle` 再算一次；至少 clamp 到 1 行，永远不会塌成 0 高 |

---

## 7. 明确不做

- 三列布局持久化（记住 sash 位置到配置文件）——本期不做，可后续加
- 预览模块加缩放/旋转控件
- 日志级别过滤、导出日志
- 路径输入的行内语法校验（非法路径在点击 File/Folder/Write 时仍然会报错）

---

## 8. 验收标准

1. 三列并排：文件 / EXIF（含日志）/ 预览，两条竖分隔条可拖
2. 中间列内：表单与日志之间一条横分隔条可拖
3. 文件列初始 ~330px，不再占 50%
4. 日志默认 4 行，新消息自动滚到底，不占整行宽度
5. 拖拽竖分隔条时预览图片按新宽度重解码，不裁切
6. 135 项测试全绿 + 新增分栏相关测试
7. 打包 exe 在 4K/200% 下 sash 可点击、布局正常
