# 文件列表表格化 + 拖拽刷选 · 优化计划（v2）

> 状态：待确认 · 目标版本 v2.2 · 基线 commit `a0b6e26`（tests: 103 passed）
> v2 变更：**硬约束——列表内不访问文件系统**（不 stat、不 open）。列内容只能来自内存。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 技术方案 | `tk.Listbox` → `ttk.Treeview`（tree column 放自绘复选框图标 + 4 个数据列） |
| 列设计 | `☑` / `#` / `文件名` / `格式` / `状态` —— 五列，**全部来自内存** |
| 数据来源 | `input_files`（字符串切片）+ `file_checked` + `bm.recs`（会话写入记录） |
| 文件 IO | **零**。不 stat、不 open、不读 EXIF，切目录瞬间完成 |
| 拖拽刷选 | press/motion/release 三阶段状态机 + `identify_row`，**设为固定值**而非翻转 |
| 性能 | 单行更新 156µs → 9µs（**17x**，实测）；identify_row 5µs/次 |
| 新增依赖 | **无**（PIL 已有，图标自绘） |
| 二期（本次不做） | 列头排序、按格式筛选、右键菜单 —— 均为零 IO 功能 |

---

## 1. 现状梳理

### 1.1 数据结构

| 成员 | 位置 | 说明 |
|---|---|---|
| `self.input_files: list[str]` | 818 | 完整路径，**列表顺序即显示顺序** |
| `self.file_checked: list[bool]` | 820 | **按下标与 input_files 严格对齐**，`_refresh_list` 时重置为全 True |
| `self.bm.recs: list[Rec]` | 463 | 会话写入日志。`Rec(src, dst, state)`，`state: pending → done / failed` |

→ 这三份数据就是表格的全部素材，**已经全部在内存里**。

### 1.2 视图层

`self.file_list = tk.Listbox`（1122-1134）：`height=6`、等宽字体、`SURFACE_2` 底色。
勾选状态**编码在行文本里**：`"■  文件名"` / `"□  文件名"`（`TICK_ON/TICK_OFF`，1697），未勾选行 `fg=TEXT_2`。

绑定（1136-1138）：

- `<ButtonRelease-1>` → `_on_list_click`：按 `nearest(y)` 命中行 → 切换勾选
- `<space>` → `_on_list_space`：按 `curselection()` 切换（独立路径，避免空格事件携带的鼠标坐标勾错行）
- `<<ListboxSelect>>` → `_schedule_preview`：100ms 防抖预览

列表 footer（1141-1151）：`list_count_lbl`（N files · 已勾选 M）+ 全选 / 全不选 / Clear。

### 1.3 调用关系图

```
_pick_file / _pick_folder / _clear_files          (1745-1780)
        └─> _refresh_list()                        (1797)
              ├─ file_list.delete(0, "end")
              ├─ file_checked = [True] * n
              ├─ 空列表 → insert 提示行, fg=TEXT_3   (1800-1806)
              ├─ 有文件 → 逐行 insert "■  basename"
              ├─ _refresh_list_footer()             (1818)
              └─ selection_set(0) + _schedule_preview()
                  ↑ Listbox 编程式 selection 不派发事件，必须显式调度

_on_list_click / _on_list_space                    (1829-1841)
        └─> _toggle_file_tick(idx)                 (1843)
              ├─ file_checked[idx] = not ...
              ├─ _render_file_row(idx)             (1786)  ← delete + insert 单行
              ├─ _refresh_list_footer()
              └─ selection_clear/set + _schedule_preview()

_tick_all_files(value)                             (1853)  → 逐行 _render_file_row

_schedule_preview                                  (1630)
        └─ after(100) → _preview_for_selection     (1725)
              ├─ curselection() → sel[0]
              └─ _show_preview(input_files[sel[0]])

_read_selected                                     (1866) → curselection() → read_exif_info
_write_async                                       (1985) → checked_files() (1860) → 后台线程
_export_csv                                        (2149) → checked_files() → read_exif_info × n
```

**`file_list` / `curselection()` 的全部引用点共 20 处**（附录 A 全量列点）。

### 1.4 测试依赖（tests/test_backup.py::TestFileTickBoxes，397-460）

| 测试 | 依赖点 | 迁移影响 |
|---|---|---|
| `test_all_files_ticked_by_default` | `file_checked` 值、`checked_files()` | 无需改 |
| `test_unticked_files_are_excluded` | 直接赋值 `file_checked[1]` | 无需改 |
| `test_row_renders_tick_square` | **`file_list.get(0).startswith("■")`** | **需改**（唯一直接断言 Listbox 文本的测试） |
| `test_toggle_flips_and_refreshes_footer` | `_toggle_file_tick(0)` + footer 文案 | 无需改 |
| `test_tick_all_and_untick_all` | `_tick_all_files` | 无需改 |
| `test_write_only_touches_ticked` | `file_checked` 直接赋值 | 无需改 |

→ **`file_checked` / `checked_files()` / `_toggle_file_tick(idx)` / `_tick_all_files(bool)` 四个 API 签名全部保留**，测试改动面 = 1 处。

---

## 2. 问题清单

| # | 问题 | 影响 | 依据 |
|---|---|---|---|
| P1 | 只有文件名一列，看不出格式、也看不出哪些已处理 | 无法判断该动哪些文件 | 用户诉求 |
| P2 | 无拖拽批量刷选 | 1000 个文件只能逐个点 | 用户诉求 |
| P3 | Listbox 改文本必须 `delete + insert` 重建整行 | 刷选 156µs/行 → 1000 行 156ms 明显卡顿 | 实测 |
| P4 | 空态占位行与真实行共用 Listbox 下标空间 | 语义隐晦（已用边界检查兜住） | 1790/1844 |
| P5 | 无列排序 | 大目录定位特定文件低效 | 二期 |

---

## 3. 优化目标（一期，可验收）

1. 文件列表呈现为表格：`☑ | # | 文件名 | 格式 | 状态`
2. 支持拖拽刷选：按住左键扫过若干行 → 统一勾选/取消（含跨滚动）
3. **列表渲染全程不触碰文件系统**——切 1000 个文件的目录也不等待
4. 四个勾选 API 签名不变，现有测试除 1 处外零改动
5. 单行状态更新 ≤ 10µs，1000 行刷选 < 20ms
6. 视觉与现有设计语言一致（自绘圆角复选框，主题蓝）

---

## 4. 目标设计

### 4.1 列定义与数据来源（逐列核对 IO）

| 列 | 宽度 | 内容 | 数据来源 | 是否 IO |
|---|---|---|---|---|
| `☑` | 34 | 自绘复选框图标 | `file_checked[i]` | 否 |
| `#` | 30 | 行号（1 基） | `enumerate` 下标 | 否 |
| `文件名` | 拉伸 | `os.path.basename(p)` | 路径字符串切分 | 否 |
| `格式` | 54 | `扩展名.upper()`（JPG/PNG/TIF） | 路径字符串切分 | 否 |
| `状态` | 66 | 就绪 / 写入中 / 已写入 / 失败 | 见 4.4 | 否 |

**被否决的列及理由**：

| 候选列 | 否决理由 |
|---|---|
| 大小 | 需 `os.stat`，远程盘 5-20ms/文件 → 1000 张 5-20 秒 |
| 尺寸 / 拍摄时间 / GPS | 需 `Image.open + getexif`，实测 22ms/张 → 1000 张 22 秒 |
| 目录 | 当前只能单文件或单文件夹导入，该列几乎总是同一个值，冗余 |
| 勾选统计 | footer 已有（`7 files · 已勾选 5`） |

```python
self.file_list = ttk.Treeview(
    list_holder, columns=("idx", "fmt", "status"), show="tree headings",
    selectmode="browse", height=8,
)
self.file_list.heading("#0",     text="")
self.file_list.heading("idx",    text="#")
self.file_list.heading("fmt",    text="格式")
self.file_list.heading("status", text="状态")
self.file_list.column("#0",     width=250, stretch=True)   # 复选框图标 + 文件名
self.file_list.column("idx",    width=30,  anchor="e", stretch=False)
self.file_list.column("fmt",    width=54,  anchor="w", stretch=False)
self.file_list.column("status", width=66,  anchor="w", stretch=False)
```

- **iid 策略**：`str(index)`，与 `input_files` 下标一一对应（`int(iid)` 即下标）
- 空态占位 iid：`HINT_IID = "__hint__"`（双下划线前缀，与数字 iid 不可能撞车）

### 4.2 复选框图标

PIL 自绘 18×18 圆角框，实例属性持有防 GC：

- 勾选：主题蓝底 + 白勾
- 未勾选：白底 + 灰边

Treeview 的 `item(image=...)` 切换（实测 9µs/行）。保留纯函数 `_tick_marker(idx) -> "■"/"□"` 供测试与日志断言，不依赖渲染层。

### 4.3 拖拽状态机（核心）

```python
def _on_press(self, e):
    iid = self.file_list.identify_row(e.y)
    if not iid or iid == HINT_IID:
        return
    self._drag_from  = iid
    self._drag_value = not self.file_checked[int(iid)]   # 固定目标值，不是翻转
    self._drag_seen  = {iid}
    self._dragging   = False

def _on_motion(self, e):
    if self._drag_from is None:
        return "break"
    iid = self.file_list.identify_row(e.y)
    if iid and iid != HINT_IID:
        if iid != self._drag_from:
            self._dragging = True
        for r in self._rows_between(self._drag_from, iid):    # 按下标取区间
            if r not in self._drag_seen:
                self._drag_seen.add(r)
                self._set_checked(int(r), self._drag_value)
    self._autoscroll(e)
    return "break"

def _on_release(self, e):
    if self._drag_from and not self._dragging:
        self._toggle_file_tick(int(self._drag_from))          # 单击语义
    self._drag_from = None
    self._refresh_list_footer()                               # 拖完统一刷一次
    return "break"
```

设计要点：

- **固定值而非 toggle**：来回拖不会刷出棋盘格（最常见的实现错误）
- **单击/拖拽分流**：以「行是否变化」判定，而非像素位移，避免手抖误判
- **footer 不实时刷**：拖动中跳过，release 时统一刷新，避免 O(n) 重复计算
- **边缘自滚**：`_autoscroll` 在指针进入上下 8px 区时 `yview_scroll(±1, "units")`

### 4.4 状态列：复用 `bm.recs`，零新增状态

`BackupManager` 本来就记录着本次会话每个写入文件的结局（`Rec.state`：`pending → done / failed`），回滚后对应记录被移除。所以状态列不需要新建任何状态机：

```python
def _status_index(self) -> dict[str, str]:
    """path -> 状态键，纯内存，O(n) 建一次"""
    if self.bm is None:
        return {}
    return {r.src: r.state for r in self.bm.recs}      # pending/done/failed

def _refresh_status_column(self) -> None:
    idx = self._status_index()
    for i, p in enumerate(self.input_files):
        st = idx.get(p)
        text, tag = {"pending": ("写入中", "writing"),
                     "done":    ("已写入", "written"),
                     "failed":  ("失败",   "failed")}.get(st, ("就绪", "ready"))
        self.file_list.set(str(i), "status", text)
        self.file_list.item(str(i), tags=(tag,))
```

刷新时机（全部是已有的事件点）：

| 时机 | 触发 |
|---|---|
| `_refresh_list()` 末尾 | 切目录 / 首次加载 |
| `_write_worker` 完成回调 | 批量写入结束 |
| `_rollback()` 之后 | 回滚结束（recs 被清） |

- 1000 行 × 9µs ≈ 9ms，可接受
- **不轮询**，只在上述三个事件点刷新
- tag 颜色：`ready`=TEXT_3、`writing`=TEXT_2、`written`=SUCCESS、`failed`=ERROR

### 4.5 选中 → 预览链路迁移

| 现状 | 目标 |
|---|---|
| `file_list.curselection()` → `sel[0]` | `file_list.selection()` → `iid`，校验 `iid.isdigit()` |
| `<<ListboxSelect>>` | `<<TreeviewSelect>>`（防抖逻辑不变） |
| `selection_set(0)` | `selection_set("0")` + 显式 `_schedule_preview()` |

`_preview_for_selection` / `_read_selected` 两处改为「iid → 下标 → 边界检查」的同一模式。**预览仍会在选中时读取图片**——但那是用户显式选中单行的动作，不是列表渲染，符合约束。

### 4.6 空态

```python
self.file_list.insert("", "end", iid=HINT_IID,
                      text="点击 File / Folder 选择图片", values=("", "", ""))
self.file_list.item(HINT_IID, tags=("hint",))
self.file_list.tag_configure("hint", foreground=Theme.TEXT_3)   # 4.76:1
```

所有 iid 入口先判 `== HINT_IID`；`_toggle_file_tick` 的边界检查保留。

---

## 5. 分步实施顺序

| 步骤 | 内容 | 验收标准 |
|---|---|---|
| **S1** | 控件骨架替换：Treeview + 5 列定义 + 样式 + `_refresh_list` 重建 + `_set_row_state` | 列表正常显示（复选框/序号/文件名/格式/就绪）、空态在、103 测试仅 1 个待改 |
| **S2** | 选中/预览链路迁移（selection / iid → 下标 / `_read_selected`） | 点行出预览、Read EXIF 正常、防抖仍生效 |
| **S3** | 勾选交互：press/motion/release 状态机 + space + `_toggle_file_tick` + `_tick_all_files` | 单击翻转、拖拽正向/反向/来回、空格键（真实事件验证） |
| **S4** | 自绘复选框图标（PIL on/off） | 图标随状态切换、截图确认 |
| **S5** | 状态列接线（`_status_index` + 三个刷新时机 + tag 配色） | 写入后行变「已写入」、回滚后回「就绪」、失败行标红 |
| **S6** | 样式打磨：行高、隔行色、hover、选中行、列宽、滚动条 | 截图对比 mockup |
| **S7** | 测试更新与新增（1 改 + 6 新） | pytest 全绿（103 + 新增） |
| **S8** | 冒烟 + 打包 + 截图归档 | exe 启动、拖拽手感、DPI 正常 |

依赖关系：S2/S3 必须在 S1 之后；S4 可与 S3 并行；S5 在 S1 之后独立；S6 在 S3 之后；S7 随各步增量补；S8 最后。

---

## 6. 潜在风险点与缓解

| # | 风险 | 概率/影响 | 缓解措施 |
|---|---|---|---|
| R1 | 选中语义迁移遗漏 `curselection` 调用点 | 中/高 | 已 grep 清点全部 3 处；改后全量测试 + 手动点行 |
| R2 | 拖拽与单击冲突（手抖误判成拖拽） | 中/中 | 以「行变化」判定 dragging，不用像素位移 |
| R3 | Treeview 内置 B1 选区行为干扰刷选 | 高/中 | `selectmode="browse"` + motion `return "break"`，只手动控制 selection |
| R4 | ~~远程盘 stat 缓慢冻结 UI~~ | — | **已由「零 IO」约束消除**，不计入风险 |
| R5 | 拖拽中 footer/预览频繁刷新卡顿 | 中/低 | 拖动中不刷 footer；预览沿用 100ms 防抖 |
| R6 | `file_checked` 下标语义被破坏 | 中/中 | 保留 `list[bool]`；iid 强制 `str(index)` |
| R7 | 空态行被当作真实行（勾选/预览） | 中/中 | `HINT_IID` 常量 + 所有入口先判 + 边界检查 |
| R8 | 右列 `bind_all` 滚轮与 Treeview 滚轮冲突 | 低/低 | 现有 handler 仅指针在右列时 break，左列不受影响；需回归验证 |
| R9 | DPI aware exe 下行高/图标尺寸偏移 | 中/低 | 行高用像素配置，打包后截图核对 |
| R10 | 复选框图标被 GC 变空白 | 中/中 | 图标存实例属性 |
| R11 | 状态列刷新时机遗漏（写入后不更新） | 中/中 | 三个刷新点显式接线；测试覆盖「写完 → 状态变已写入」 |

---

## 7. 明确不做（边界）

- 二期：列头排序（按文件名/格式）、按格式筛选、右键菜单——**均为零 IO 功能**
- **不做任何列的文件 IO**：不 stat、不 open、不读 EXIF（含二期；若未来需要，必须用户显式触发）
- 不引入第三方表格库（保持零新依赖）
- **不改**写入 / 备份 / 回滚 / 预览解码逻辑

---

## 8. 预期收益

| 指标 | 现状 | 一期后 |
|---|---|---|
| 单行状态更新成本 | 156 µs | 9 µs（17x） |
| 可见信息列 | 1 | 5 |
| 1000 文件勾选操作 | N 次点击 | 1 次拖拽 |
| 切目录耗时 | 同步建立列表 | 不变（本来就没有文件 IO） |
| 处理后结果可见性 | 只有日志 | 逐行状态列 + 回滚自动复位 |

---

## 附录 A：`file_list` / `curselection` 引用点全量清单

| 行号 | 代码 | 迁移动作 |
|---|---|---|
| 1122-1138 | Listbox 创建 + 3 处 bind | 换 Treeview + `<<TreeviewSelect>>` |
| 1719-1723 | `_preview_for_selection` 的 curselection | selection() + iid 校验 |
| 1786-1796 | `_render_file_row` | → `_set_row_state(idx, checked)` |
| 1797-1816 | `_refresh_list` | 重建 item + HINT_IID |
| 1829-1841 | `_on_list_click` / `_on_list_space` | 拆成 press/motion/release + space |
| 1843-1851 | `_toggle_file_tick` | 内部改 `_set_row_state`，签名不变 |
| 1853-1858 | `_tick_all_files` | 遍历 iid，签名不变 |
| 1866-1874 | `_read_selected` | selection() + iid 校验 |
