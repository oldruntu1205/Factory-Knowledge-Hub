VERDICT: CHANGES_REQUESTED

MUST_FIX:
- **② 读快照不一致**：READ COMMITTED 下，revision 校验后仍可能发生写入或 generation 切换；后续统计、ACL、候选可能来自不同快照。显式 generation 不能解决跨语句一致性，旧 generation 删除还可能使后续查询读不到行 -> 将统计加载、ACL 判定及候选查询纳入同一个请求级只读 REPEATABLE READ 事务，且在首条查询前设置。无需退回“统计只做离线”。
- **⑥ 权限异常后的回退未闭环**：若统计查询在显式事务内报 InsufficientPrivilege，仅捕获异常会留下 aborted transaction，随后 live 查询无法执行 -> 用 savepoint 包住统计加载，异常后回滚到 savepoint 再回退；补“旧角色无 SELECT 权限仍成功返回 live 结果”的事务级测试。若已有此处理，提供证据即可。
- **③ 并发重建缺少关键验证**：状态行锁和原子发布方向正确，但 REPEATABLE READ 下，等待期间状态行被其他事务修改，可能触发 serialization failure；不能保证所有竞争都表现为“等待后继续” -> 对 40001 重试整个重建事务，并补双连接定向测试，覆盖写入先行、重建先行及两个重建竞争，验证无错误 READY、无部分发布。
- **recall 验收证据不足**：统计相等和“算法未改”支持等价性判断，但尚未直接证明目标中的 recall 不低于 v3 -> 补同数据、同主体、同查询集的 v3 对照；可用候选及最终排序等价证据，或既有 recall 基准。

SHOULD_FIX:
- **① revision 合并策略可接受，但须明确事务契约**：每事务每 tenant 一次，足以表示已提交源数据发生变化；前提是覆盖全部影响统计的写入，tenant 迁移须使 OLD/NEW 两侧失效。禁止同一事务先 bump、再 refresh、随后继续修改源数据，否则 GUC marker 可能使 built=source 错误保持成立。
- **③ 隔离级别应成为函数契约**：`refresh_tenant` 应检查或明确拒绝不满足 REPEATABLE READ 的调用，避免正确性仅依赖 CLI。记录重建期间同 tenant 写入会被状态行锁阻塞。
- **④ ACL 加总有条件成立**：分组必须互斥，每个统计文档只计一次；同组判定须完全由分组键、主体及同快照 policy 决定。仅相同 policy_id 不能证明不存在资源级差异。补该不变量及对应测试；满足这些条件且 policy 修改触发失效时，无需把 policy revision 加入分组键。
- **⑤ 冗余 docs 可以接受，但当前约束较弱**：CHECK 只保证 `df <= term.docs`；普通分组 FK 不保证 `term.docs = corpus.docs`。若要数据库强制该不变量，可对 corpus 的“分组键 + docs”建 UNIQUE，再用包含 docs 的复合 FK 引用。
- **⑦ 性能表述应限定条件**：可称“同一 100k 合成数据、刷新成功且统计有效时，本次测量达到 p50/p95 目标”。“需刷新”还须包含“刷新后无使其失效的提交”；任何此类写入都会重新进入未达标的 live 路径。

BOUNDARY:
- 本轮仅审查送审文本，未核验代码、测试输出或证据文件。
- 不要求本轮补并发负载、冷缓存或生产规模性能验收；上述双连接正确性测试仍属必要修复。
- 不要求自动刷新调度、增量维护或重构 v3；失效回退 live 可以保留，但不能宣称整体请求路径或生产 SLO 已达标。