# 湖表对象发布边界设计（DESIGN）

本文只描述设计，不修改 `objects.py`、`lake.py`、`catalog.py`、样例、观察器与断言。所有源码引用均带有效行号，证据由 `python3 -B review.py verify` 重放核对（见第 7 节）。

## 1. 基线事实与缺陷定位

### 1.1 现有公开签名与持久化原语（保留项）

- `Objects(root=None)`、`Objects.put(fid, rows)`、`Objects.get(fid)`：`objects.py:7`、`objects.py:13`、`objects.py:30`。`put` 的校验、规范字节生成与两个错误串 `BAD_FILE_ID`（`objects.py:14-15`）、`IMMUTABLE_CONFLICT`（`objects.py:24`）、内存模式分支（`objects.py:25-28`）均保持不变；磁盘模式只改“字节如何到达最终名字”。
- `Lake.seed/read/delete/prepare/publish/rewrite`：`lake.py:10`、`lake.py:18`、`lake.py:27`、`lake.py:39`、`lake.py:56`、`lake.py:68`。公开签名、返回结构与错误串全部保留。
- `Catalog.write/head/state/commit`：`catalog.py:22`、`catalog.py:36`、`catalog.py:39`、`catalog.py:48`；`commit` 仍要求处于写事务内（`catalog.py:49-50`），快照、文件集、墓碑、head 的行结构不变（`catalog.py:53-57`）。
- 规范 JSON 的生成参数不变：`json.dumps(..., sort_keys=True, separators=(",", ":"), allow_nan=False).encode()`（`objects.py:16`）。因此“同名同内容”等价于同一段规范字节，幂等与冲突判定都以字节相等为准，不引入解析后的语义比较。

### 1.2 基线三个发布边界缺陷

1. **最终名字直接承载半成品。** 磁盘 `put` 用 `path.open("xb")` 在最终名 `fid.json` 上创建并直接写载荷（`objects.py:18-21`）。`write` 可能部分完成（CONTRACT 故障模型），观察器 `SplitWrite` 在首次写入中途 flush 前半段并暂停（`observe.py:50-56`），此时 `get` 解析半截字节得到 `JSONDecodeError`（`observe.py:118-119` 实测）。最终名一旦存在却不完整，违反“对象名只能完整可见”。
2. **无任何持久化屏障。** `objects.py:19-21` 只有 open/write/close，没有 `fsync`；`order` 时序实测 `python_fsync` 事件为空（`review.py:31` 断言）。close 不是屏障（CONTRACT 明示），所以即便目录事务提交，被引用对象的数据与目录项也可能尚未落盘；断电后可能出现“快照引用的名字缺失/内容不全”。
3. **半成品永久毒化最终名，且跨载荷持有 SQLite 写事务。** `cut_write` 实测：进程被终止后，重开仍是不可解析前缀，同内容重试因最终名已存在且字节不等而报 `IMMUTABLE_CONFLICT`（`observe.py:120-123`），名字无法自愈。另一方面，同步 `rewrite` 在 `with self.catalog.write()` 内做 `prepare`（`lake.py:69-70`），而 `prepare` 内部对每个输出组调用 `objects.put` 写文件（`lake.py:52`）；`order` 实测该对象写入的 `in_transaction=true`（`review.py:30`）。这正是被禁止的“跨对象载荷写入持有 SQLite 写事务”。

### 1.3 目录侧已具备的保证（沿用，不重复解决）

`Catalog` 使用 SQLite DELETE journal 与 `synchronous=FULL`（`observe.py:22-23` 固定设置），在 CONTRACT 假设下事务具备原子性与持久性：`write()` 的 BEGIN IMMEDIATE/COMMIT/ROLLBACK 边界见 `catalog.py:22-34`。因此中断重开后目录事务“完整存在或未提交”由 SQLite 自身保证（`before_commit` 实测 head=0、`after_commit` 实测 head=1 即其表现，见第 6 节）。SQLite 提交不替对象文件做屏障（CONTRACT 明示），对象侧屏障必须由本设计显式完成。

## 2. 设计目标与不变量

- **V1 原子可见性：** 最终名 `root/<fid>.json` 只能由“同目录原子改名”或“原子无覆盖创建/链接”产生；任何读者经 `Objects.get`（`objects.py:30-32`）看到的最终名，其字节要么完整可解析，要么名字不存在（`get` 抛 `FileNotFoundError`，属于“未发布名字允许暂时不存在”）。读者永不观察到半截字节。
- **V2 单胜者与不可变：** 最多两个并发同名写入者（CONTRACT 限定）。同名同规范字节，双方在 I/O 成功时都成功（幂等）；同名异字节，恰有一个内容胜出，另一方得到 `IMMUTABLE_CONFLICT`，且任何路径都不覆盖胜者。判定只用字节相等（复用 `objects.py:23` 的比较形态）。
- **V3 先屏障后引用：** 目录事务提交前，其将引用的每个对象都已完成“文件 fsync + 目录 fsync”两道屏障；无论对象由本次写入、先前重试还是竞争者创建，都要满足（采用边界见第 5 节）。屏障或写入失败时 `put` 抛错、不返回成功；`seed/publish` 因对象缺失/异常而失败时不提交目录。
- **V4 目录事务原子、对象可遗留：** 对象与目录不要求同一原子事务；对象先完成而目录未提交可留下不可达对象，不做回收/清理/压实/迁移。不跨载荷持有 SQLite 写事务。
- **V5 历史读取不变：** 任一实际存在的快照都能完整读取，历史内容不变（`read` 排序与墓碑过滤逻辑 `lake.py:18-25` 不动）；中断重开后快照完整存在或未提交。
- **V6 无持久任务标识：** 重试只依据公开的名字与内容规则，不增加任务表、计划持久化或恢复器；`prepare/publish` 仍不持久保存计划（与 CONTRACT 对 `publish` 的说明一致）。

## 3. 两种可行的对象发布策略

两种策略都把载荷先写到**同目录临时名**，屏障后再经原子原语发布到最终名，从而消除缺陷 1/2；差异在“最后一步用哪个原语”以及同名竞争如何收敛。临时名只在对象根目录内、带随机唯一后缀，永不被 `get` 或目录引用，遗留临时文件属于允许的不可达对象，不自动清理。

### 策略 A：临时文件 + 检查后 rename（同目录原子改名）

要点：普通 `rename` 不能假定为无覆盖（CONTRACT 明示），所以不能直接 rename 到最终名，否则异内容会静默覆盖胜者，破坏 V2。必须在 rename 前消除同名竞争窗口。

- 临时名形如 `.<fid>.tmp-<rand>`。写全量载荷、`flush`、文件 `fsync(f)`（文件数据与必要属性持久）、关闭。
- 最终名处理需要一个**同名串行化手段**（例如对规范化后的 `fid` 取一个 `fcntl.flock` 辅助锁文件，锁仅覆盖“检查—改名—复核”这一小段，不覆盖载荷写入，也不是 SQLite 事务）：
  1. 持锁后若最终名不存在 → `rename(tmp, final)`（同目录原子改名）→ 对目录 `fsync(dirfd)` 持久化该名字变更 → 解锁；
  2. 若最终名已存在 → 读回字节：相等则采用（删除自己的临时文件，目录 fsync 见第 5 节）后成功；不等则删除临时文件、抛 `IMMUTABLE_CONFLICT`，**绝不 rename 覆盖**；
  3. 改名后再复核一次最终名字节，若与预期不符（理论上仅在锁失效/外部干预时）按冲突处理。
- 优点：只依赖原子 rename，语义直观；临时文件与最终文件是同一个 inode，改名后无需再对文件本身 fsync。
- 缺点：正确性依赖一个额外的同名锁来弥补 rename 可覆盖的语义；锁文件本身的创建/遗留、锁粒度与异常释放都要设计；在“最多两个写入者”下虽可行，但竞争胜负取决于锁调度，且必须用“存在性 + 字节复核”双重确认，状态更多。

### 策略 B：临时文件 + 原子无覆盖硬链接（推荐）

要点：直接利用 CONTRACT 明确保证的“原子无覆盖创建/链接”原语（Linux 本地文件系统 `os.link(tmp, final)`，最终名已存在时原子失败 `FileExistsError`），把竞争收敛交给一次系统调用，无检查—改名窗口，无需辅助锁。

- 写临时文件并完成文件屏障；随后 `os.link(tmp, final)` 无覆盖发布；成功后对目录 `fsync(dirfd)` 持久化新名字，再 `unlink(tmp)`（unlink 也在同一目录，目录 fsync 已先完成；遗留亦可，unlink 仅为整洁，失败不影响正确性）。
- `link` 抛 `FileExistsError` 时进入**采用/拒绝分支**（第 5 节）：读回最终名字节，相等则采用并成功，不等则抛 `IMMUTABLE_CONFLICT`，全程不触碰胜者内容。
- 优点：单条原子原语决定胜负，天然无覆盖、无 TOCTOU、无需额外锁；两个并发写入者的结局由 link 的原子性严格确定（同内容双赢、异内容一胜一拒）；临时文件始终不在最终名上，读者永远看不到半成品。
- 缺点：依赖硬链接原语（CONTRACT 已将其列为受支持假设，明确“不要求兼容缺少这些原语的平台”）；采用他人对象时需要一次读回与一次目录 fsync 来闭合持久性论证（第 5 节）。

### 3.1 推荐与理由

**推荐策略 B。** 它把 V1/V2 的核心保证锚定在单次“原子无覆盖 link”上，而策略 A 必须再引入一个同名锁并做改名后复核才能抵消普通 rename 的覆盖语义。在本契约限定的“一个目录发布者、最多两个对象写入者、本地 FS、无恶意修改”模型下，B 的必要状态最少（仅临时文件 + 最终名，无锁文件）、竞争胜负可由系统调用原子性直接证明，且与现有 `open("xb")` 的无覆盖意图（`objects.py:20`）一脉相承，只是把“在最终名上无覆盖创建并直接写”改成“在临时名上写、屏障后无覆盖链接发布”。下文伪代码均以策略 B 为准，并在第 5 节给出策略 A 的对应采用边界。

## 4. 推荐方案的必要状态与伪代码

### 4.1 `Objects.put` 的磁盘形态（签名与错误串不变，替换 `objects.py:17-24` 的磁盘分支）

新增的持久化辅助（设计层面，不要求落地为新模块）：

```text
# 屏障顺序严格为：写全量 -> 文件 fsync -> 原子发布 -> 目录 fsync
fsync_file(path):
    with open(path, "rb") as f: os.fsync(f.fileno())      # 数据+必要属性
fsync_dir(dir_path):
    fd = os.open(dir_path, os.O_RDONLY)
    try: os.fsync(fd) finally: os.close(fd)               # 持久化此前相关名字变更
```

`put` 伪代码（内存模式仍走 `objects.py:25-28`，不变）：

```text
put(fid, rows):
    校验 fid（沿用 objects.py:14-15，非法 -> BAD_FILE_ID）
    payload = 规范字节（沿用 objects.py:16，参数不变）
    if 内存模式: 沿用 objects.py:25-28 的字典比较/赋值；return
    final = root / (fid + ".json")
    # 快速幂等路径：最终名已存在且字节相等，直接采用（采用边界见第 5 节）
    if final.exists():
        existing = final.read_bytes()
        if existing == payload: adopt(final, root); return
        raise ValueError("IMMUTABLE_CONFLICT")            # 不覆盖、不创建临时文件
    # 临时名只在同目录，保证后续 link/rename 是同文件系统原子操作
    tmp = root / (".%s.tmp-%s" % (fid, uuid4_hex))
    try:
        fd = os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, 0o644) # 唯一名，避免临时名互撞
        try:
            写全部 payload（处理短写/异常，出错即抛，不宣称成功）
            os.fsync(fd)                                   # 屏障 1：文件数据与属性
        finally: os.close(fd)                              # close 本身不是屏障
        try:
            os.link(tmp, final)                            # 原子无覆盖发布（V1/V2）
        except FileExistsError:
            existing = final.read_bytes()                  # 竞争者已发布
            if existing == payload: adopt(final, root)     # 同内容：采用，成功
            else: raise ValueError("IMMUTABLE_CONFLICT")   # 异内容：拒绝，胜者不动
        else:
            fsync_dir(root)                                # 屏障 2：新名字持久化
        return
    except BaseException:
        raise                                              # 写入/屏障失败一律抛错（V3）
    finally:
        静默 unlink(tmp)（若仍存在；遗留亦允许，unlink 失败不改变成败判定）
```

`get`（`objects.py:30-32`）逻辑不变：它只打开最终名。临时名对 `get` 不可见；最终名由 link 原子产生，故 `get` 要么读到完整字节并 `json.loads` 成功，要么因名字不存在而 `FileNotFoundError`，不再可能出现基线的 `JSONDecodeError`。

### 4.2 采用（adopt）的边界——既有同名对象何时可以“认下”

`adopt` 同时用于快速路径与 link 失败路径，规则对“本次写入、先前重试、竞争者创建”三种来源一致：

```text
adopt(final, root):
    existing = final.read_bytes()          # 任何读失败 -> 抛错，不降级为成功
    # 边界 1：只采用对象根目录内的最终名 <fid>.json；
    #         绝不采用任何 .tmp 临时名，也不引用根目录之外的路径。
    # 边界 2：仅当 existing == 本次规范 payload 才可采用；字节不等即 IMMUTABLE_CONFLICT。
    # 边界 3：采用者自己没有执行过该名字的“文件 fsync + 目录 fsync”。
    #         胜者在 link 成功后已做目录 fsync（4.1），其文件 fsync 更早已完成；
    #         为让“采用者返回成功”也闭合 V3，采用者在 read_bytes 成功后再做一次
    #         fsync_dir(root)：若该名字变更本就持久，这是幂等空操作；若尚未持久，
    #         此调用把它补持久。不重写、不覆盖胜者文件，故无需再对文件 fsync。
    fsync_dir(root)
```

采用边界的解释：

- **只认字节，不认身份。** 采用不需要知道胜者是谁，也不需要任务标识（V6）。规范字节相等即内容等价（第 1.1 节），所以采用竞争者对象与采用自己先前重试留下的对象，在正确性上无差别。
- **异内容绝不采用、绝不覆盖。** 字节不等立即 `IMMUTABLE_CONFLICT`，且本方案没有任何 rename/replace 路径能改写已存在的最终名（link 与 O_EXCL 都是无覆盖），胜者内容在 V2 下不可变。
- **持久性闭合。** 采用者成功返回前，该最终名已被一次成功的目录 fsync 覆盖；配合胜者完成的文件 fsync，满足“被引用对象已完成设计要求的持久化屏障”，即使对象并非本次调用所写。若设备在屏障前掉电，按 CONTRACT 该名字变更可能不存在，此时重试会重新走临时文件 + link 发布，仍收敛到同一字节，不产生分叉。
- **策略 A 的对应边界：** 仅在持同名锁、确认最终名不存在时才 rename；锁内发现最终名已存在时同样只读回比较，相等才采用并补目录 fsync，不等即冲突，禁止任何覆盖式 rename。

### 4.3 `seed`：先全部对象屏障，再单事务提交（替换 `lake.py:10-16` 的组织方式，签名/返回不变）

```text
seed(files):
    for fid, rows in files.items():
        objects.put(fid, rows)        # 每个对象都在事务外完成 4.1 的双屏障
    with catalog.write():             # BEGIN IMMEDIATE（catalog.py:26）
        if catalog.head() != 0:       # 沿用 lake.py:14
            raise ALREADY_SEEDED      # write() 自动 ROLLBACK（catalog.py:31-33）
        # 提交前防御性确认：本快照将引用的名字在对象根中全部存在（V3）
        for fid in files:
            if not (objects.root / (fid+".json")).exists():
                raise RuntimeError("OBJECT_NOT_PERSISTED")   # 事务回滚，不报成功
        return catalog.commit(set(files), set())   # 沿用 catalog.py:48-58
```

要点：所有载荷写入与 fsync 都在 `catalog.write()` 之外，SQLite 写事务只覆盖“校验 + 插入快照行 + head 更新”这一短区间。`put` 已成功即意味着双屏障完成；提交前的存在性检查是纵深防御（对象由外部删除不在威胁模型内，此检查主要捕获编程错误与未屏障路径），失败则回滚，绝不提交引用缺失对象的快照。`ALREADY_SEEDED` 仍在事务内判定，行为与基线一致（对象可能已遗留，符合 V4）。

### 4.4 `prepare` / `publish`：对象写入与目录事务分离（沿用 `lake.py:39-66` 的判定，调整屏障与事务位置）

```text
prepare(job, inputs, layout):
    # 全程不开启 catalog.write；所有读走 catalog.state()（catalog.py:39）
    base, files, dead = catalog.state()
    校验 inputs 互异/非空/属于快照（沿用 lake.py:41-42，BAD_INPUTS）
    读取 inputs 载荷并校验 layout 恰好覆盖全部物理位置一次（lake.py:43-47，BAD_LAYOUT）
    outputs = []
    for i, group in enumerate(layout):
        fid = "rewrite-%s-%d" % (job, i)          # 沿用 lake.py:50 命名
        rows = [过滤墓碑后的行]                     # 沿用 lake.py:51
        objects.put(fid, rows)                    # 事务外完成双屏障（4.1）
        outputs.append(fid)
    return {"base": base, "inputs": sorted(inputs), "outputs": outputs}  # 沿用 lake.py:54
```

```text
publish(plan):
    with catalog.write():                         # 短事务，不跨任何文件写入
        sid, files, dead = catalog.state()
        if sid != plan["base"]: raise PUBLISH_CONFLICT   # 沿用 lake.py:59-60
        inputs = set(plan["inputs"])
        if not inputs <= files: raise BAD_PLAN          # 沿用 lake.py:61-63
        # 提交前确认新快照引用的每个对象（保留集 + 输出集）都已持久存在（V3）
        referenced = (files - inputs) | set(plan["outputs"])
        for fid in referenced:
            if not (objects.root / (fid+".json")).exists():
                raise RuntimeError("OBJECT_NOT_PERSISTED")   # 回滚，不报成功
        return catalog.commit(referenced, 过滤掉输入的墓碑)   # 沿用 lake.py:64-66
```

`plan` 仍不持久化（V6）；`prepare` 后崩溃只留下不可达输出对象（V4），重试 `prepare` 因同名同字节而幂等成功（4.1）。`publish` 的冲突与错误语义、快照号 `head+1`（`catalog.py:52`）完全不变。

### 4.5 `rewrite`：兼容的锁范围调整（`lake.py:68-71`）

公开方法签名、返回值与快照内容不变，仅把“跨 prepare/publish 的外层写事务”拆开，消除 `in_transaction=true` 的载荷写入（缺陷 3），满足“不跨载荷持有 SQLite 写事务”：

```text
rewrite(job, inputs, layout):
    plan = prepare(job, inputs, layout)     # 事务外：读快照 + 写对象（含双屏障）
    try:
        return publish(plan)                # 短事务：仅目录状态切换
    except ValueError as e:
        if str(e) == "PUBLISH_CONFLICT":
            raise                          # 与基线同错误串；调用方可按契约重新 prepare
        raise
```

兼容性论证：

- 单线程/无并发目录更新（CONTRACT：目录更新之间不并发）时，`prepare` 读到的 head 到 `publish` 之间不会被他人推进，`PUBLISH_CONFLICT` 行为与基线一致；`checks.py:14` 的同步 rewrite 断言（返回快照 2、当前顺序 `[7,9,7,11]`、快照 1 仍为 `[7,7,9,11]`，见 `checks.py:15-16`）保持成立。
- `Catalog.write()` 的嵌套分支（`catalog.py:24-25`、`catalog.py:29`、`catalog.py:32`）保留，因此“公开方法独立调用、不讨论调用方持外层事务”的契约不受影响；本设计只是 `Lake.rewrite` 自身不再持有外层事务。
- 两个对象写入者并发作用于同名 rewrite 输出时，由 4.1 的 link 原子性收敛（同内容双赢、异内容一胜一拒），不会把半成品带进任何快照。

### 4.6 异常与重试矩阵（无持久任务标识）

| 失败点 | 调用结局 | 磁盘残留 | 重试行为（仅靠 fid+内容） |
|---|---|---|---|
| 临时文件写中途进程终止 | `put` 未返回，不成功 | 仅 `.tmp-*`，最终名不存在 | 新临时名重写 → link 成功；旧临时文件为允许的不可达对象 |
| 文件 fsync 失败/出错 | 抛异常，不 link、不成功 | 可能有临时文件 | 重试同上；最终名从未出现，无毒化 |
| `link` 遇 `FileExistsError`，字节相等 | 采用，成功返回 | 胜者最终名 + 自己的临时文件（随后 unlink/遗留） | 再次调用走快速幂等路径，仍成功 |
| `link` 遇 `FileExistsError`，字节不等 | `IMMUTABLE_CONFLICT`，胜者不动 | 胜者最终名 + 自己的临时文件 | 同内容重试才可能成功；异内容永远冲突，符合不可变契约 |
| 目录 fsync 失败 | 抛错，不宣称成功（即使 link 已生效） | 最终名可能已在目录中 | 重试时快速路径读到相等字节 → adopt 补一次目录 fsync 后成功 |
| `seed` 对象阶段失败 | 异常，未进入目录事务 | 已写对象（可能已屏障） | 重试 seed：同名同字节幂等；head 仍为 0 时提交快照 1 |
| `seed` 事务内 `ALREADY_SEEDED` | 错误串不变，事务回滚 | 对象可遗留 | 与基线一致，不产生新快照 |
| `prepare` 后、`publish` 前终止 | 无快照变更 | 不可达输出对象 | 重跑 prepare 幂等；再 publish |
| `publish`/`commit` 中途进程终止 | SQLite 事务完整或未提交（1.3） | 输出对象已屏障 | 重开后按 head 判定：未提交则重新 publish；已提交则冲突路径报错，与基线 `after_commit` 同构 |

## 5. 读取可见点与历史保证

- **可见点唯一。** 一个对象名对 `Lake.read`（`lake.py:18-25`）可见，当且仅当某已提交快照的 `files` 含该 fid（`catalog.py:43`）且对象根存在完整最终名。对象名的“内容可见点”是 `os.link` 原子返回成功（或快速路径读到已存在最终名）；在此之前只有临时名，`get` 不可见。link 之后任何读者读到的都是完整规范字节。
- **快照完整性。** 提交前检查（4.3/4.4）+ 对象双屏障 + SQLite FULL 提交，保证任一实际存在的快照引用的对象都完整可读；`read` 的排序、墓碑过滤（`lake.py:21-24`）与历史快照分支（`catalog.state(sid)`，`catalog.py:39-46`）不改，历史内容字节不可变（V2），`NO_SNAPSHOT`（`catalog.py:41-42`）等行为保留。
- **只读者无锁。** 读者不参与 link/锁，不需要协调；最坏情况是名字尚不存在（`FileNotFoundError`，对应“未发布名字允许暂时不存在”），不会读到半截 JSON。

## 6. 六个公开时序：原行为（实测）、拟议结果（预测）与断电推论（假设）

说明：`actual` 为 `python3 -B observe.py all` 的真实输出（第 7 节可重跑）；`predicted` 是本设计在相同时序注入点下的**设计预测**，未伪造执行；`basis` 标注所依赖的操作顺序与假设。SIGKILL 只证明进程终止后重开（不清空 OS 缓存），断电结论另列，且逐项依赖 CONTRACT 的屏障/诚实设备假设。

### 6.1 same / different（对象竞争，`observe.py:113-133`）

- **实测（基线）：** 暂停点处最终名 `shared.json` 已含半截前缀，`during_get` 为 `JSONDecodeError`；竞争者 `put` 因最终名已存在且（写入者尚未写完）字节不等而 `IMMUTABLE_CONFLICT`；放行写入者后重开读取成功；same 同内容重试 `{"ok": null}`，different 异内容重试仍冲突。
- **拟议预测：** 暂停点只存在临时名，最终名不存在 → `visible_prefix` 形态的半截最终名不再出现，`during_get` 变为“名字不存在”（`FileNotFoundError`）而非 `JSONDecodeError`。
  - same：竞争者先完成 link 发布完整 ROWS 并目录 fsync；原写入者 link 时 `FileExistsError`，读回字节相等 → 采用成功。双方均成功，重开 `get` 得 ROWS，重试幂等成功。
  - different：一方 link 成功成为唯一胜者；另一方 link 失败后读回字节不等 → `IMMUTABLE_CONFLICT`，胜者内容不被覆盖；异内容重试仍冲突，同内容（若改回 ROWS）才可成功。
- **断电推论：** 若掉电发生在文件 fsync 之后、目录 fsync 之前，胜者的 link 名字变更可能丢失（CONTRACT：未被成功屏障覆盖的变更可丢），但临时文件数据已持久；重开后最终名不存在，任一方重试可重新 link，字节仍收敛一致，不会出现两个内容。若掉电发生在目录 fsync 成功之后，最终名与数据均持久。以上依赖“同目录原子 link、文件/目录 fsync 语义、设备诚实执行屏障”，不由 SIGKILL 实验证明。

### 6.2 cut_write（写入中途被杀，`observe.py:120-123`）

- **实测（基线）：** 重开 `reopened_get` 仍为 `JSONDecodeError`，同内容重试 `IMMUTABLE_CONFLICT`——最终名被半截字节毒化且无法自愈。
- **拟议预测：** 被杀时只可能留下 `.tmp-*`（进程终止不保证其持久，但它永不被引用），最终名 `shared.json` 不存在 → 重开 `get` 是“名字不存在”而非解析错误；同内容重试以新临时名重写并 link 成功，`retry` 为 `{"ok": null}`，毒化消除。
- **断电推论：** 即使临时文件因缺少屏障而部分/全部丢失，也不影响最终名空间；重试重新生成完整字节。不存在“半截最终名持久化”的路径，因为最终名只能由 link 引入且引入前文件已 fsync。

### 6.3 before_commit（对象写完、进入 `Catalog.commit` 前被杀，`observe.py:74-77`、`observe.py:136-146`）

- **实测（基线）：** head=0，`read` 快照 0 为空；对象 `shared.json` 可读；重试 seed 成功发布快照 1，`after_retry` 可读两行。
- **拟议预测：** 目录侧结论不变（SQLite DELETE/FULL 事务未提交即不存在，head 保持 0）。对象侧差异：本设计下对象在 seed 进入事务前已完成双屏障，因此对象“可读”不仅是进程终止后的缓存现象，而是有屏障支撑；重试 seed 时 `put` 走快速幂等路径（字节相等、adopt 补目录 fsync），随后事务提交快照 1，结局与基线同构（head=0 → 重试后快照 1）。
- **断电推论：** 基线下对象无 fsync，断电后对象可能丢失或不全，重试 seed 需重新写；本设计下若两道屏障均成功则断电后对象完整持久，若屏障未完成则对象可能缺失，重试重新发布——两种情况目录都停在快照 0，不会引用缺失对象。

### 6.4 after_commit（目录事务完成、响应暂停时被杀）

- **实测（基线）：** head=1，当前 `read` 与对象 `get` 均成功；重试 seed 报 `ALREADY_SEEDED`。
- **拟议预测：** 完全保持：SQLite 事务已提交（FULL 下持久），对象双屏障在提交前已完成，故快照 1 在重开/断电后都完整可读；重试 seed 仍在事务内命中 head!=0 → `ALREADY_SEEDED`（`lake.py:14-15`）。
- **断电推论：** 这是本设计中持久性论证最强的一点：目录提交（SQLite FULL）与对象屏障（文件 fsync + 目录 fsync）都先于进程终止点；依赖诚实设备，断电后快照 1 与其引用对象同时存活。SIGKILL 实测只覆盖“重开可读”，断电存活是屏障假设的推论。

### 6.5 order（操作顺序与事务范围，`observe.py:149-198`）

- **实测（基线）：** seed 与分段 prepare 的对象写入 `in_transaction=false`，同步 rewrite 的对象写入 `in_transaction=true`；无 `python_fsync` 事件；快照 1 为 ROWS 原序、当前为反转序。
- **拟议预测：** 业务结果不变（快照号、当前 `[9,7]`、历史 `[7,9]` 全部保持，`checks.py:14-16` 回归通过）。可观察差异有两处：(1) 每个对象写入序列变为 `临时名 open/write/close → 文件 fsync → link → 目录 fsync → unlink 临时名`，因此出现 `python_fsync` 事件且最终名上没有 open-for-write；(2) 同步 rewrite 的对象写入移出事务，三个对象写入阶段的 `in_transaction` 全部为 `false`，SQL 侧只在 `publish` 短事务内出现一对 BEGIN IMMEDIATE/COMMIT。
- **断电推论：** 事件观测来自 Python 层包装（CONTRACT：观察器看不到 SQLite 内部系统调用，也不能把缺少 python_fsync 推断为 SQLite 未同步）；新增的 python 层 fsync 是对象屏障的显式证据，其断电效果仍按假设推论，不在本时序实测范围内。

## 7. 可重放证据与验证入口

- 真实观察入口（保持不变）：`python3 -B observe.py all`（单时序可替换 `all` 为 `same`/`different`/`cut_write`/`before_commit`/`after_commit`/`order`）。
- 业务回归 + 文档核对：`python3 -B review.py verify`——先跑 `checks.check()`（`checks.py:10-37`）与全部观察，再核对下方证据块的 `actual` 与真实输出逐字节相等、`predicted`/`basis` 非空、三个生产模块的 `文件:行号` 引用有效（`review.py:38-54`）。它不判定策略正确性，竞争与持久性推理仍需人工审阅。
- 回放文档证据：`python3 -B replay.py --input examples/move.json` 与 `python3 -B replay.py all` 保持原样，审计标签语义不受本设计影响。
- 证据块中所有 `actual` 均由上述 `observe.py all` 在本机重跑生成，未手工改写；`predicted`/`basis` 为设计文字。

<!-- publication-evidence -->
~~~json
{
  "after_commit": {
    "actual": {
      "after_retry": {
        "rows": [
          {
            "file": "shared",
            "pos": 0,
            "value": {
              "reading": 7
            }
          },
          {
            "file": "shared",
            "pos": 1,
            "value": {
              "reading": 9
            }
          }
        ],
        "snapshot": 1
      },
      "head": 1,
      "object": {
        "ok": [
          {
            "reading": 7
          },
          {
            "reading": 9
          }
        ]
      },
      "read": {
        "rows": [
          {
            "file": "shared",
            "pos": 0,
            "value": {
              "reading": 7
            }
          },
          {
            "file": "shared",
            "pos": 1,
            "value": {
              "reading": 9
            }
          }
        ],
        "snapshot": 1
      },
      "retry_seed": {
        "error": "ValueError",
        "message": "ALREADY_SEEDED"
      }
    },
    "basis": "依据 catalog.py:48-58 的提交序列在单个 SQLite 事务内更新 snapshots/files/tombstones/head，FULL 配置下提交即原子持久；拟议协议保证提交引用的每个对象都已完成文件与目录 fsync（含竞争者对象的采用边界：只读回比对、不重写、补目录 fsync）。SIGKILL 实测覆盖重开可读；断电后快照与对象同时存活是屏障假设的推论。",
    "predicted": "结果与基线一致并获得持久性支撑：目录事务已随 SQLite FULL 提交，重开 head=1，当前 read 与对象 get 均成功；对象双屏障在提交前已完成，因此快照 1 引用的对象完整存在；重试 seed 在事务内命中 head!=0 仍报 ALREADY_SEEDED，历史快照内容不变。"
  },
  "before_commit": {
    "actual": {
      "after_retry": {
        "rows": [
          {
            "file": "shared",
            "pos": 0,
            "value": {
              "reading": 7
            }
          },
          {
            "file": "shared",
            "pos": 1,
            "value": {
              "reading": 9
            }
          }
        ],
        "snapshot": 1
      },
      "head": 0,
      "object": {
        "ok": [
          {
            "reading": 7
          },
          {
            "reading": 9
          }
        ]
      },
      "read": {
        "rows": [],
        "snapshot": 0
      },
      "retry_seed": {
        "ok": 1
      }
    },
    "basis": "依据 catalog.py:22-34 的 BEGIN IMMEDIATE/COMMIT/ROLLBACK 边界与 DELETE journal、synchronous=FULL 的原子持久性假设，未提交事务重开后不存在；拟议 seed（lake.py:10-16 调整形态）把全部对象 put 与双屏障放在事务外，提交前再做引用对象存在性确认，失败即回滚不报成功。SIGKILL 实测 head=0；对象在断电后是否存活取决于屏障是否成功，属逐项假设推论。",
    "predicted": "目录侧结果与基线同构：SQLite DELETE journal 加 synchronous=FULL 的事务尚未提交，重开 head 仍为 0、快照 0 为空；对象侧因 put 在进入目录事务前已完成文件 fsync 与目录 fsync，shared 对象完整可读且有屏障支撑；重试 seed 时同名同字节走幂等采用路径，随后短事务提交快照 1，after_retry 读到两行，全过程不持有跨载荷的 SQLite 写事务。"
  },
  "cut_write": {
    "actual": {
      "during_get": {
        "error": "JSONDecodeError",
        "message": "Expecting ',' delimiter: line 1 column 15 (char 14)"
      },
      "reopened_get": {
        "error": "JSONDecodeError",
        "message": "Expecting ',' delimiter: line 1 column 15 (char 14)"
      },
      "retry": {
        "error": "ValueError",
        "message": "IMMUTABLE_CONFLICT"
      },
      "visible_prefix": "[{\"reading\":7}"
    },
    "basis": "依据最终名只能由原子 link 引入、link 前已完成文件 fsync 的顺序，进程在临时写入阶段终止时最终名空间不受影响；CONTRACT 允许遗留不可达对象且不要求临时文件清理，故残留 .tmp 不违反契约；重试无持久任务标识，仅按公开 fid 与内容规则重新发布。断电推论同此：缺失屏障的临时数据可丢，最终名要么完整存在要么不存在。",
    "predicted": "拟议协议下进程在临时文件写入中途被 SIGKILL，最终名从未被 link 创建，重开 get 表现为名字不存在而非 JSONDecodeError，半截字节只可能残留在不被任何读者或快照引用的 .tmp 临时名上；同内容重试以新临时名重写、文件 fsync、link 发布并目录 fsync，retry 成功返回 {\"ok\": null}，基线的最终名毒化与永久冲突被消除。"
  },
  "different": {
    "actual": {
      "contender": {
        "error": "ValueError",
        "message": "IMMUTABLE_CONFLICT"
      },
      "during_get": {
        "error": "JSONDecodeError",
        "message": "Expecting ',' delimiter: line 1 column 15 (char 14)"
      },
      "reopened_get": {
        "ok": [
          {
            "reading": 7
          },
          {
            "reading": 9
          }
        ]
      },
      "retry": {
        "error": "ValueError",
        "message": "IMMUTABLE_CONFLICT"
      },
      "visible_prefix": "[{\"reading\":7}"
    },
    "basis": "依据 link 的原子无覆盖创建语义与 objects.py:23-24 既有的字节比较/IMMUTABLE_CONFLICT 规则，胜负由一次系统调用决定，无检查-改名窗口；方案不含 rename 覆盖或 O_TRUNC 路径，胜者内容不可变。断电只可能丢失未被成功屏障覆盖的名字变更，不可能让两份异内容同时可见，此为屏障与原子 link 假设下的推论，不由 SIGKILL 证明。",
    "predicted": "拟议协议下暂停点最终名不存在，读者看不到半截前缀；两个写入者字节不同，link 的原子无覆盖语义保证恰一方发布胜者，另一方 FileExistsError 后读回字节不等，得到 IMMUTABLE_CONFLICT 且不存在任何覆盖胜者的代码路径；重开 get 读到胜者完整内容，异内容重试持续冲突，只有改为与胜者相同的规范字节时才可幂等成功。"
  },
  "order": {
    "actual": {
      "current": {
        "rows": [
          {
            "file": "rewrite-sync-0",
            "pos": 0,
            "value": {
              "reading": 9
            }
          },
          {
            "file": "rewrite-sync-0",
            "pos": 1,
            "value": {
              "reading": 7
            }
          }
        ],
        "snapshot": 3
      },
      "events": [
        {
          "fid": "a",
          "in_transaction": false,
          "op": "object_open"
        },
        {
          "bytes": 29,
          "fid": "a",
          "in_transaction": false,
          "op": "object_write"
        },
        {
          "fid": "a",
          "in_transaction": false,
          "op": "object_close"
        },
        {
          "op": "BEGIN IMMEDIATE"
        },
        {
          "op": "COMMIT"
        },
        {
          "fid": "rewrite-staged-0",
          "in_transaction": false,
          "op": "object_open"
        },
        {
          "bytes": 29,
          "fid": "rewrite-staged-0",
          "in_transaction": false,
          "op": "object_write"
        },
        {
          "fid": "rewrite-staged-0",
          "in_transaction": false,
          "op": "object_close"
        },
        {
          "op": "BEGIN IMMEDIATE"
        },
        {
          "op": "COMMIT"
        },
        {
          "op": "BEGIN IMMEDIATE"
        },
        {
          "fid": "rewrite-sync-0",
          "in_transaction": true,
          "op": "object_open"
        },
        {
          "bytes": 29,
          "fid": "rewrite-sync-0",
          "in_transaction": true,
          "op": "object_write"
        },
        {
          "fid": "rewrite-sync-0",
          "in_transaction": true,
          "op": "object_close"
        },
        {
          "op": "COMMIT"
        }
      ],
      "history": {
        "rows": [
          {
            "file": "a",
            "pos": 0,
            "value": {
              "reading": 7
            }
          },
          {
            "file": "a",
            "pos": 1,
            "value": {
              "reading": 9
            }
          }
        ],
        "snapshot": 1
      }
    },
    "basis": "依据 observe.py:149-198 的 Python 层包装仅能观测文件操作与 SQL 事务边界、看不到 SQLite 内部系统调用；拟议调整对应 lake.py:68-71 的 rewrite 拆分为事务外 prepare（lake.py:39-54）加短事务 publish（lake.py:56-66），Catalog.write 嵌套分支 catalog.py:22-34 保留以兼容独立公开调用；新增 python_fsync 是对象屏障的显式观测点，其断电效果仍依赖 CONTRACT 的 fsync 与诚实设备假设，不把观测事件本身当作断电证据。",
    "predicted": "业务结果保持：仍生成快照 1、2、3，当前读取为反转序 [{\"reading\":9},{\"reading\":7}]，快照 1 保持原序 [{\"reading\":7},{\"reading\":9}]，checks.py 原回归通过；可观察差异是每个对象写入变为 临时名 open/write/close、文件 python_fsync、原子 link、目录 fsync、临时名 unlink，且同步 rewrite 的对象写入移出目录事务，三个对象写入阶段 in_transaction 全为 false，SQL 仅在 publish 短事务内出现 BEGIN IMMEDIATE/COMMIT 配对。"
  },
  "same": {
    "actual": {
      "contender": {
        "error": "ValueError",
        "message": "IMMUTABLE_CONFLICT"
      },
      "during_get": {
        "error": "JSONDecodeError",
        "message": "Expecting ',' delimiter: line 1 column 15 (char 14)"
      },
      "reopened_get": {
        "ok": [
          {
            "reading": 7
          },
          {
            "reading": 9
          }
        ]
      },
      "retry": {
        "ok": null
      },
      "visible_prefix": "[{\"reading\":7}"
    },
    "basis": "依据 objects.py:16 的规范字节定义，同内容即同字节；拟议 put 以同目录临时名承载写入（write 可部分完成但临时名不可见），成功文件 fsync 后用 CONTRACT 保证的原子无覆盖 link 发布，再做目录 fsync；FileExistsError 分支只读回比较，字节相等即采用并补一次目录 fsync 闭合持久性。SIGKILL 实测仅证明进程终止后重开且不清空 OS 缓存；断电下 link 名字变更可能因目录 fsync 未完成而丢失，但重试用同一字节重新 link 仍收敛，该结论依赖诚实设备与 fsync 语义假设而非实验。",
    "predicted": "拟议协议下暂停点只有同目录临时名，最终名 shared.json 尚不存在，during_get 不再是 JSONDecodeError 而表现为名字不存在；两个写入者规范字节相同，先完成文件 fsync 与原子无覆盖 link 的一方发布胜者并做目录 fsync，另一方 link 得到 FileExistsError 后读回字节相等而采用（adopt），双方均成功返回；重开 get 得到完整 ROWS，再次 put 走快速幂等路径仍成功，任何时刻最终名只暴露完整字节。"
  }
}
~~~
