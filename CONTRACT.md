# 湖表对象发布边界讨论基线

## 输入、调用与已有结果

这是 Python 标准库与 SQLite 的小型数据湖。objects.py 存规范 JSON 行文件；catalog.py 管理快照、位置墓碑和 head；lake.py 串起两者。讨论对象是这三个实际文件，不包括旧题的采样分析功能。
Objects(root=None) 为内存模式；磁盘模式用 Objects("work/objects")。调用者先创建 work；Catalog("work/catalog.db") 与 Objects 共同构造 Lake。讨论假设对象根目录和初始目录库已建立并持久化，初始化建目录不在范围内。
Objects.put(fid,rows) 接收名字和可规范序列化的 JSON 行数组，返回 None；规范字节由 sort_keys=True、separators=(",",":")、allow_nan=False 的 json.dumps 后 encode 产生。fid 非空且只含英文字母、数字、减号、下划线，否则 BAD_FILE_ID。同名同规范字节可重复调用，同名异内容为 IMMUTABLE_CONFLICT。Objects.get(fid) 返回原行数组。未发布的名字允许暂时不存在；最终名字一旦可见，内容就应完整且以后不变。

Lake.seed(files) 接收 fid 到行数组的映射，空库发布快照1；非空库报 ALREADY_SEEDED，快照0为空。
Lake.read(snapshot=None) 固定当前 head 或指定历史快照，输出 {"snapshot":整数,"rows":[{"file":名字,"pos":零起位置,"value":行}]}，按文件名和位置排序，排除相应位置墓碑；未知快照报 NO_SNAPSHOT。
Lake.delete(snapshot,fid,pos) 先验证历史快照和位置；非法位置为 BAD_POSITION，当前已移除该文件为 STALE_LAYOUT。重复删除不新增快照；首次删除产生下一快照。
Lake.prepare(job,inputs,layout) 读取当前快照；inputs 必须互异、非空且属于该快照，否则 BAD_INPUTS。layout 是输出组数组，每组含 [fid,pos]，恰好列出输入文件所有物理位置一次，墓碑也要列入，否则 BAD_LAYOUT。输出过滤墓碑，文件名为 rewrite-JOB-组号，允许空组。返回 {"base":整数,"inputs":[名字],"outputs":[名字]}。
Lake.publish(plan) 检查 head 等于 base，否则 PUBLISH_CONFLICT；输入不属于当前文件集为 BAD_PLAN。成功替换文件集并移除输入墓碑，新快照号为原 head+1。计划由 prepare 产生且不篡改，不要求持久保存计划或自动恢复调用。
Lake.rewrite(job,inputs,layout) 是同步封装，当前跨 prepare/publish 持目录写事务。本题允许调整这一内部锁范围，但正常返回值、快照内容及原回归要保留。公开方法独立调用，不讨论调用方持有外层 Catalog.write 的用法。

samples.py 的两个 reading=7 属于不同物理行。正常重写后当前 reading 顺序为 [7,9,7,11]，快照1仍为 [7,7,9,11]。checks.py 的原断言覆盖这些语义和已有发布冲突、旧位置拒绝；本题不修复或改造删除协议。
replay.py --input examples/move.json 接收 {"files":映射,"operations":数组}，输出带原始位置审计标签的操作轨迹；python3 -B replay.py all 可查看四种已有时序。

## 本轮设计约束与故障模型

只有一个目录发布者，允许最多两个独立进程同时对同一对象名调用 put，另有只读者。目录更新之间不并发；并发对象写入者遵守拟议协议。不考虑恶意修改、外部文件删除、介质腐坏或网络文件系统。
设计应使最终对象名不暴露半成品；两个同名同内容写入在均不中断且 I/O 成功时均能成功，同名异内容只能由一个内容胜出，另一方拒绝且不能覆盖胜者。
成功的目录事务引用的每个对象都已完成设计要求的持久化屏障；无论对象由本次写入、先前重试还是竞争者创建，都必须解释这个保证。发生写入或屏障错误时不能宣称成功，但不要求实现通用 I/O 故障恢复器。
对象先完成而目录未提交可以留下不可达对象；不要求对象与目录在同一原子事务内，也不要求自动恢复进程中断的操作。不能通过跨对象载荷写入持有 SQLite 写事务来解决发布边界。
中断重开后，目录事务应完整存在或未提交；任一实际存在的快照都应能完整读取，历史内容保持不变。进程终止后重试按已公开名字和内容规则处理，不增加持久任务标识。
方案不涉及对象回收、临时文件自动清理、在线压实、迁移、多个目录发布者或分布式存储。

支持的假设：单机本地文件系统提供同目录原子改名和原子无覆盖创建/链接；不要把普通 rename 假定为无覆盖操作。write 可能部分完成，close 不是持久化屏障；成功的文件 fsync 保证该文件数据与必要属性持久，成功的目录 fsync 保证此前相关名字变更持久。存储设备诚实执行屏障，断电可以丢失未被成功屏障覆盖的变更。请明确自己实际依赖的操作、返回条件和顺序，不要求兼容缺少这些原语的平台。
磁盘观察器固定使用 SQLite DELETE journal 与 synchronous=FULL；假设 SQLite 及文件系统满足此配置的事务原子性与持久性契约。SQLite 提交并不替对象文件完成屏障。观察器只记录 Python 层文件操作和 SQL 事务边界，不能观测 SQLite 内部系统调用。
SIGKILL 验证的是进程终止后重开；它不清空操作系统缓存，不能作为断电持久性的实验证明。断电结论必须逐项依赖明确假设，不需要真实断电或开发文件系统模拟器。

## 可重跑的原行为观察

python3 -B observe.py all 每行输出 {"case":名字,"actual":观察对象}。all 可替换为单个名字。
same 与 different：子进程调用原 Objects.put，在第一次写入的中途由观察包装器写出并 flush 规范字节的前半段，然后通过管道暂停。父进程分别用相同或不同内容竞争同名对象，随后放行写入者，重开读取并重试。ROWS=[{"reading":7},{"reading":9}]；OTHER 只将首个 reading 改为8。
cut_write：使用相同暂停点，终止写入子进程，再读取及重试。before_commit：seed 对象写入结束，进入 Catalog.commit 但尚未修改快照时终止。after_commit：seed 完成目录事务但响应仍被暂停时终止；二者重开目录后读取并重试 seed。
order：磁盘运行 seed、prepare/publish 和同步 rewrite，记录 object_open、object_write、object_close、Python 层 fsync 及 SQL BEGIN IMMEDIATE/COMMIT/ROLLBACK；文件事件带 in_transaction。返回当前读取及快照1读取。记录不等于原生 I/O 跟踪，不将缺少 python_fsync 推断为 SQLite 没有做内部同步。
观察器用临时工作目录、管道及受控暂停点，不靠 sleep 猜时序。异常返回 {"error":异常类型,"message":原文}；正常调用返回 {"ok":原返回值}。JSONDecodeError 是基线半成品的可观察结果，不是目标设计应保留的业务错误。
基线预期：same 的竞争者遇到 IMMUTABLE_CONFLICT，完成后同内容重试成功；different 的竞争及重试均冲突。cut_write 重开仍是不可解析前缀且同内容重试冲突。before_commit 的 head=0、对象可读，重试发布快照1；after_commit 的 head=1、对象可读，重试 seed 报 ALREADY_SEEDED。order 中 seed 与分段 prepare 的对象写入在事务外，同步 rewrite 在事务内；这些只是原行为。
python3 -B review.py baseline 运行原业务回归并核对全部上述观察；它成功说明基线可复现，不表示发布协议安全。
运行清单的 demo 为 python3 -B observe.py all，展示真实观察结果。

## 讨论交付

交付带 objects.py、catalog.py、lake.py 有效源码行号的 DESIGN.md，串起 put、seed、prepare、publish、rewrite 与 commit。比较两种可行的对象发布策略并推荐一种，写清必要状态、竞争胜负、读取可见点、持久确认、目录提交及异常重试的关键伪代码。必须解释既有同名对象的采用边界和同步 rewrite 锁范围的兼容处理；不要求编写新对象存储或修改业务源码。
每个公开观察时序都记录原行为、拟议结果及理由，区分实际测量、设计预测和断电假设。预测应说明读者何时看到完整内容或缺失、哪个调用成功或冲突、中断重开后的目录和对象如何解释；无需伪造执行拟议实现的输出。
在 DESIGN.md 放置 <!-- publication-evidence --> 后接一个 ~~~json 代码块；顶层键为 observe.py all 返回的全部 case 名字。每项有 actual（该时序完整原始观察对象）、predicted（非空文字）和 basis（非空文字，解释所依赖的顺序及假设）。
保留业务源码、样例、观察器和原断言，交付时将运行清单 verify 改为 ["python3","-B","review.py","verify"]。
python3 -B review.py verify 重跑基线，核对文档 actual 与真实输出一致、预测和理由存在、三个生产模块的源码行号有效。它不判定策略或伪代码正确；竞争与持久性推理仍须人工审阅。demo 保持真实观察入口。
