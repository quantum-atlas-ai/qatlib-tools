# Lean 证明复核与公理审计

Lean 的可靠性建立在“证明搜索”与“内核验算”彻底解耦之上：战术脚本、元程序乃至 AI 搜索过程尽可以反复试错，内核只对最终提交到环境 `Γ` 中的声明与证明项做类型检验（`Γ ⊢ p : P`）。不过，一次完整的工程验收不止于编译通过——复核者还需要确认命题 `P` 的定义与假设忠实表达了目标问题，并厘清整个结论究竟建立在哪些公理、预编译工件和检查器实现之上。

## 五类常见质疑与验证边界

围绕大型形式化项目与 AI 辅助证明的争议，往往混淆了“内核管什么”与“规格审计管什么”。下表将五类典型风险拆解为底层机制、工程防线与实际保证范围：

| 常见质疑与风险场景 | Lean 验证机制与审查边界 | 工程审计与防线做法 | 实际保证范围与边界 |
| --- | --- | --- | --- |
| **搜索幻觉与隐蔽占位**：AI 极易产生幻觉，仅在源码里搜不到 `sorry` 是否仍可能漏掉未完成证明？ | 证明搜索与内核判卷彼此独立，中间幻觉只要无法构造出合法证明项就会被内核拦截；但纯文本搜索查不到跨模块传递带入的 `sorryAx`、自定义 `axiom`，或由 `native_decide` 引入的编译器信任公理。 | 从编译后的 `Environment.constants` 按模块归属提取全部导出声明，沿类型与证明体收集传递公理闭包，严格比对三条标准公理白名单；同时单独排查 `unsafe`、`@[implemented_by]` 与 `@[extern]`。 | 可完整锁定证明链条的公理依赖与审计范围内的运行时替换入口；纯源码文本检索只能说明字面未出现某关键字，不能替代环境级公理审计。 |
| **题面失真与空真陷阱**：逻辑推导每一步都合规，是否仍可能把概念改义、暗塞强前提，或证出空集上的“空真命题”？ | 内核只检验“证明项是否匹配当前写出的类型”，不判断该类型是否忠于原问题。改写基础定义、`section` / `variable` 混入过强或矛盾前提、定义域退化为空集，都可以做到公理列表完全干净。 | 锁定上游依赖提交与对应工件，完整审阅导出签名；通过库外普通 `module` 消费探针与 `lake comparator` 锁死目标定义，再配合环境 lint 排查冗余假设，并给出满足全部前提的非空实例见证（witness）。 | 能确保目标命题在给定定义与接口下严格成立；至于定义、量词和前提本身是否准确刻画了原数学问题，仍须由题面规格审阅与实例见证共同把关。 |
| **检查器软件缺陷**：底层的依值类型论逻辑一致，但编写内核程序的工程师会不会写出软件漏洞？ | 数学逻辑无矛盾不等于单一软件实现零缺陷——例如元编程生成的复杂嵌套归纳声明，历史上就曾暴露过内核对游离变量与元变量检查的遗漏。 | 固定已包含相关安全修复的工具链版本，构建后先用官方 `leanchecker --fresh` 从空环境重放，再导出声明交由 `lean4lean`、`nanoda`、`con-leche` 等独立实现的检查器交叉验算。 | 多套独立编写的检查器对同一份导出同时放行，可将单点实现缺陷漏检的概率压到极低；导出链路、共享运行时及题面规格本身仍须纳入整体信任评估。 |
| **中间代码退化或仅靠 `rfl`**：局部辅助谓词被写成 `True`、中间等式只靠 `rfl` 展开，是否意味着整套证明“注水造假”？ | 篡改终点命题才会破坏结论；把中间跳板改成 `True` 后，仍须硬证 `True → R` 才能抵达真正的目标 `R`，难度并未减少；带前提的引理若前提无法满足也消不去条件，而 `rfl` 只在两侧按定义严格相等时才会被内核接受。 | 单一目标定理冲刺优先锁死终点题面与传递公理；而面向下游复用的公共定理库中，每条公开引理本身都是对外契约，须逐模块审查其类型非退化性、代表性实例与实际可调用性。 | 只要终点题面锁死且多检查器重放通过，局部笨拙或退化的辅助代码就无法动摇终点定理的真值；但公共库若要保证模块级复用质量，仍须逐条把关导出接口。 |
| **代码通过与论文背书的界限**：十几万行代码没人逐行读完，或者配套自然语言论文被指出步骤有偏差，机器证明还算数吗？ | 形式证明的数学效力由内核对证明项与题面的机器检验确立，不以人类先读懂全部证明脚本为前提；但反过来，“代码证出了定理 `T`”并不会自动替配套 PDF 论文里的每一段自然语言推导或更强的渐近常数背书。 | 建立论文命题到 Lean 声明的显式映射表，逐条标明精确等价、假设收窄、常数或阶数差异，以及尚未形式化的推论。 | 凡已建立准确映射并通过审计的形式化声明，其数学真值均可独立复核；论文中超出代码声明范围的文字结论，仍需补齐对应定理或单独审阅论证。 |

> 历史案例参考：Lean 官方内核曾针对元编程构造嵌套归纳类型时的游离变量与元变量漏检合入专门修复。高可信复核应确保源码提交、依赖清单与预编译工件严格对齐，并在报告中记录所用工具链及各独立检查器的具体版本。[Lean 内核修复 #14607](https://github.com/leanprover/lean4/pull/14607) · [Lean 4.35 发布说明](https://lean-lang.org/doc/reference/4.35.0/releases/v4.35.0/)

## 环境级传递公理与零 `sorry` 审计

开展审计前，先固定 `lean-toolchain`、`lake-manifest.json` 与待审源码提交，并核清目标模块及上游依赖工件的来源。在受控构建阶段，可通过 `lake build --wfail` 将本次编译产生的警告直接提升为错误；但增量构建不会回头重报已缓存依赖中的历史源码警告，因此正式门禁必须在编译完成后直接审查加载进内存的 `Environment`。关于缓存分层与单文件验证方式，参见[编译与缓存](build.md)。

交互式排查时常用 `#print axioms Some.theoremName` 抽检单条定理；自动化门禁则应遍历 `Environment.constants`，按模块归属筛出受管模块内的全部常量，逐一调用 `Lean.collectAxioms` 收集其类型与证明体（含私有辅助引理）所依赖的传递公理闭包。在 Lean 4.35 的 `module` 体系下，跨模块引用会读取导入工件预先记录的传递公理摘要，即便下游普通 `import` 隐藏了定理的私有证明体，公理审计依然能够穿透模块边界追出底层依赖。当然，摘要本身建立在输入工件可信的前提之上；若需防范工件篡改，应结合后文的独立内核重放一并核验。[Lean 4.35 公理收集实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/Lean/Util/CollectAxioms.lean)

标准经典数学证明的公理白名单仅包含三条基础公理——命题外延性 `propext`、选择公理 `Classical.choice` 与商类型公理 `Quot.sound`（具体声明可只依赖其中任意子集）。所有显式留空或因类型错误由编译器合成的占位证明都会汇总为 `sorryAx`；只要传递闭包中出现白名单之外的任何公理，门禁即刻拒绝。值得注意的是，Lean 4.35 中的 `native_decide` 与 `decide +native` 会在每次原生求值时动态生成一条专属公理，从而显式标明该结论将编译器与运行时代码纳入了可信计算基（早期版本使用的固定公理名 `Lean.ofReduceBool` 与 `Lean.trustCompiler` 已在 4.35 中移除；排查历史工件时仍可按旧名溯源，而白名单机制能天然拦截新旧两类原生求值公理）。[Lean 4.35 发布说明](https://lean-lang.org/doc/reference/4.35.0/releases/v4.35.0/) · [原生求值实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/Lean/Elab/Tactic/Decide.lean)

除公理外，`unsafe` 声明以及 `@[implemented_by]`、`@[extern]` 运行时替换属性需要单独设防。虽然安全模式下的内核证明无法直接引用 `unsafe` 定义，且运行时外部绑定不会改写内核看到的逻辑定义体，但一旦公开接口兼具可执行计算语义，就必须同步核查这些声明的安全标记与外部替换属性。若还需限制上游依赖中的运行时覆盖，则应沿完整工件的类型、定义体与编译器替换边做全图扫描，并按精确常量名维护经人工审定的豁免清单。下方最小示例在传递公理层面贯穿全部上游依赖，同时直接拒绝目标项目模块内出现任何 `unsafe` 或运行时替换声明。

### 可运行的最小审计示例

在空目录中将 `lean-toolchain` 固定为 `leanprover/lean4:v4.35.0`，保存以下 `lakefile.toml`：

```toml
name = "verification-example"
defaultTargets = ["ExampleLib"]

[[lean_lib]]
name = "ExampleLib"
```

编写待审模块 `ExampleLib.lean`：

```lean
module

@[expose] public def step (n : Nat) : Nat := n + 1

public theorem step_pos (n : Nat) : 0 < step n := by
  exact Nat.zero_lt_succ n
```

编写环境审计入口 `Audit.lean`。此处特意省略文件头的 `module` 关键字，以便审计脚本能够看见受检项目模块内部的私有声明；接入实际仓库时，还应显式导入未被顶层聚合模块覆盖的独立文件，并区分纯重导出聚合模块与应当包含实质声明的业务模块：

```lean
import Lean
import ExampleLib

open Lean Elab Command

run_cmd liftCoreM do
  let env ← getEnv
  let allowed := #[`propext, `Classical.choice, `Quot.sound]
  let modules := #[`ExampleLib]  -- List all audited modules and import them above.
  let mut covered : Std.HashSet Name := {}
  let mut checked := 0
  for (name, info) in env.constants.toList do
    let some idx := env.getModuleIdxFor? name | continue
    let owner := env.header.modules[idx.toNat]!.module
    unless modules.contains owner do continue
    covered := covered.insert owner
    checked := checked + 1
    for ax in ← collectAxioms name do
      if ax == `sorryAx then
        throwError "{name}: depends on sorryAx"
      unless allowed.contains ax do
        throwError "{name}: unexpected axiom {ax}"
    if info.isUnsafe || (Compiler.getImplementedBy? env name).isSome ||
        (getExternAttrData? env name).isSome then
      throwError "{name}: unsafe declaration or runtime override"
  for owner in modules do
    unless covered.contains owner do
      throwError "no declarations audited from {owner}"
  IO.println s!"AXIOM_AUDIT_OK declarations={checked}"
```

执行构建与免重编环境审计：

```sh
lake build --wfail
lake --no-build lean Audit.lean
```

CI 门禁应以命令退出码为唯一放行依据，同时在日志中留存受审模块范围、扫描常量总数及首个违规常量名；若流程拆分输出 `SORRY_FREE_OK` 与 `AXIOM_AUDIT_OK` 两项回执，须确保各自在对应检查完整通过后才打印。上线门禁时，建议配套一组负向回归夹具（如跨文件私有引理暗藏 `sorry`、自定义 `axiom`、`native_decide` 求值及 `@[implemented_by]` 替换），实测确认每一类违规都会触发非零退出。

## 声明接口审查与外部消费探针

公理干净只能说明“没有借用额外假设”，要防范题面退化或接口不可用，还需在 `lake lint` 与 `lake test` 中接入声明级静态与环境检查：

- **导入、风格与环境假设审查**：在项目的 `lake lint` 驱动中集成导入层级、命名规范与跨模块重名扫描（Lean 4.35 亦支持通过 `lake lint --builtin-only ExampleLib` 直接运行编译器内建检查器；引入 Batteries 的项目可进一步启用 `unusedArguments`、`simpNF`、`dupNamespace` 等规则）。编译完成后按模块归属扫描导出常量类型，拦截结论字面退化为 `True`、前提字面含 `False`，以及由 `section`、`variable` 或 `include` 意外带入的冗余参数与死类型类约束。由于语法级规则只能识别最显式的退化，涉及复杂前提组合的核心定理仍应附带具体实例见证，证明所有假设能够同时满足。
- **全库聚合冲突检测与普通 `module` 消费探针**：在配置了 `testDriver` 的仓库中，让 `lake test` 将各顶层聚合模块导入同一环境，第一时间暴露不同子模块公开同名声明时的命名冲突；随后在库外以普通 `module` 编写下游消费探针。在 Lean 4.35 中，`public` 仅导出名称可见性，唯有标记 `@[expose]` 的定义才允许下游在类型检查阶段按定义展开——只写 `#check` 往往发现不了漏标 `@[expose]` 造成的计算退化。将下方探针存为 `Consumer.lean` 并执行 `lake --no-build lean Consumer.lean`，即可同时核验名称导出、类型签名与计算展开契约：

```lean
module
import ExampleLib

#check @step_pos
example : ∀ n : Nat, 0 < n + 1 := @step_pos
example : step 0 = 1 := rfl
#print axioms step_pos
```

当验收目标是某一固定数学猜想或基准命题时，还应将参考定义与目标签名保存在候选分支无权修改的独立规格包中，通过 `lake comparator` 自动比对候选实现与冻结题面，从机制上杜绝通过微调定义或别名“改题凑证”。[Lean 4.35 模块系统](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/) · [Lean 4.35 发布说明](https://lean-lang.org/doc/reference/4.35.0/releases/v4.35.0/)

<a id="build-gate-recovery"></a>

## 编译门禁与自动化提交恢复

将上述检查接入自动化验证与提交流水线时，还需兼顾词法查重精度、门禁日志脱敏安全与网络抖动后的幂等恢复：

1. **公开声明首尾成对预筛**：执行跨文件公开声明查重时，先提取待查名称的“首个命名空间分量 + 末段声明名”作成对预筛，命中后再比对完整限定名称确认冲突；对不含命名空间的顶层声明，则直接按声明名预筛。预筛全程按完整 Lean 标识符匹配，自动跳过注释、字符串字面量与局部子串命中，从而避免仅因共享外层命名空间就把无关的宏实现源码送入严格的静态声明扫描器。需要注意，由宏动态展开生成的声明以及真实导入阶段触发的命名冲突，仍须交由编译器检查兜底。
2. **门禁日志脱敏且保留定位上下文**：对外回传或归档门禁日志前，须遮蔽各类敏感凭据，并将宿主机私有绝对路径统一改写为仓库相对路径；与此同时，向执行侧返回的摘要必须保留失败阶段、目标模块、源码文件与行号，以及编译器原始诊断中的可操作报错信息。上线脱敏规则时，应使用同时包含模拟凭据与真实编译错误的混合日志夹具校验，确保抹除机密后仍能准确定位故障点。
3. **本地回执—提交—远端发布三段幂等恢复**：本地门禁全部通过后，先原子写入一份与当前待提交内容严格绑定的本地回执，记录受检目标、验证结论与日志路径；创建 Git 提交前再次核对待提交内容与回执一致，随后生成提交并记录其提交 SHA；最后推送分支，并按来源分支（head branch）查询已有 PR 加以更新，不存在时再新建。若推送或平台 API 因瞬时故障中断，恢复时应先依次核对本地提交、远端分支引用与 PR 状态，直接从首个未完成的阶段继续重试，避免盲目重跑已通过的本地构建；一旦工作区或待提交内容发生变化，原回执立即失效，必须重新执行门禁。

## 独立内核重放与多检查器交叉验算

日常增量构建关心的是“哪些模块需要重新编译”，而内核重放（kernel replay）解决的是“彻底绕开战术执行、宏展开与增量缓存，把已生成的 `.olean` 声明放进全新空环境从头再审一遍”。重放前先基于锁定输入完成构建，并确认目标及依赖 `.olean` 均位于检查器可解析的搜索路径下（若部分模块仅停留在 Lake 共享构件缓存中，需先恢复至标准搜索路径）：

```sh
lake env leanchecker --fresh ExampleLib
lake check --paranoid
```

`lake env leanchecker --fresh ExampleLib` 调用工具链自带的官方二进制，从零环境重放目标模块及其全部依赖（注：仅当项目在 `lakefile` 中显式声明了同名可执行目标时才使用 `lake exe leanchecker`；此外，内核重放本身视合法的 `axiom` 声明为合规输入，因此它与上一节的公理白名单审计互为补充，不能互相替代）。[Lean 4.35 检查器源码](https://github.com/leanprover/lean4/blob/v4.35.0/src/LeanChecker.lean)

在支持 Bubblewrap 沙箱的 Linux 环境中，`lake check --paranoid` 会构建默认目标、导出声明级中间表示，并自动串联官方内核与随附检查器复核。更高规格的发布验收可将同一份 NDJSON 导出同时送入用 Lean 自身重写的 [`lean4lean`](https://arxiv.org/abs/2403.14064)，以及完全独立实现的外部检查器 [`nanoda`](https://github.com/ammkrn/nanoda_lib) 与 [`con-leche`](https://github.com/leanprover/con-leche)，一并归档导出文件哈希、各检查器版本号与核验结论。若单独对外部传入文件运行 `lake check --from-export FILE --paranoid`，所得结论仅对该导出文件本身负责；要将其追溯回特定源码提交，还须配套保存受控构建与导出的完整溯源记录。[Lean 4.35 发布说明](https://lean-lang.org/doc/reference/4.35.0/releases/v4.35.0/)
