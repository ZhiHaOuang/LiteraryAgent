# Codex 基线与 LG 派生 Runtime

当前上游基线是 `rust-v0.154.0`，commit `6b9826e3aa83b1a5947db50f4332cb9c65f1b340`。
`CODEX_CORE_COMMIT` 和打包资源中的同名 pin 继续标识上游版本，不用于冒充 LG 派生版本。
`core/codex` 保持原样，文学业务留在产品层。父仓库通过 submodule 跟踪 LiteraryAgent，实际源码维护在嵌套的 LiteraryAgent 仓库内完成。

## 本地核验与明确更新

在 LiteraryAgent 根目录运行：

```bash
scripts/update_codex_core.sh --verify
scripts/update_codex_core.sh --check
```

未设置 `LG_CODEX_REF` 时，两者均不访问网络。校验比较上游 commit tree 与已提交的 `core/codex` tree，并拒绝该前缀内已跟踪或未忽略的未跟踪改动。忽略的构建产物不属于源码校验范围。
未指定目标的 `--apply` 会拒绝执行，不再自动跟随最新 release。

需要比较一个明确选定的上游版本时：

```bash
LG_CODEX_REF=rust-v0.154.0 scripts/update_codex_core.sh --check
```

这里的版本只是当前基线示例，替换为经过研究的目标版本。显式比较会执行 fetch；`--check` 不修改工作树，但会更新本地 Git 对象和 FETCH_HEAD。
`LG_CODEX_REMOTE` 默认为 fetch-only 的 `upstream`。更新前需将现有工作整理成可恢复的基线，不得覆盖其他未提交工作。

```bash
LG_CODEX_REF=rust-v0.154.0 scripts/update_codex_core.sh --apply
```

只有工作树干净时允许 apply，使用 `git merge -s ort -Xsubtree=core/codex --no-ff --no-commit`，更新两处 pin，并验证暂存的上游树。默认留下未提交结果供审查；`--commit` 为显式可选项。本命令维护原始上游基线，不能用来直接覆盖精简后的派生源码。

## 两类运行产物

`runtime_manifest.py` 负责本地身份校验，不管理文学上下文、作品状态或作者决定。

| 清单 | 用途 | 校验 |
| --- | --- | --- |
| schema 1 | 兼容已安装的上游二进制 | commit、tag/version、文件 SHA-256、版本探测；不接受派生 kind |
| schema 2 / upstream-unpatched | 新安装的上游二进制 | 上述字段，以及 source_commit/tree、build_profile、protocol |
| schema 2 / lg-derived | 本地派生二进制 | 上游 pin、独立源码 commit/tree、构建配置、协议、二进制 SHA-256、独立版本探测 |

派生清单允许 LG 自己的版本字符串，不再要求冒充 `codex-cli`。当前适配协议是 `codex-app-server-v2`；声明协议不代表全部能力已验证，仍须运行协议/工具/恢复集成测试。

正常运行配置 `[runtime].manifest` 或 `LG_CODEX_RUNTIME_MANIFEST`。清单错误时直接失败，不尝试其他运行时。
未提供清单时，仅显式设置的 `LG_CODEX_COMMAND` 可作为开发/测试入口；它只做可执行性及版本探测，不具有来源锁定保证。无这两种配置时不搜索 PATH、不启动 Node 包装器，也不临时运行 Cargo。打包部署应使用清单。

## 安装与登记

上游安装仍使用现有命令，要求本地 tag 与 pin 一致，并校验 release asset 摘要：

```bash
python scripts/install_codex_runtime.py --tag rust-v0.154.0 --destination /path/to/runtime-directory
```

安装命令需要网络；本轮不重新下载或替换已经验证的二进制。

派生构建在 LitIsLand 的独立源码 Git 仓库中进行，保留从上游 pin 开始的历史。源码修改提交后，再登记已经构建好的二进制：

```bash
python scripts/register_codex_runtime.py --source /path/to/clean-codex-source --binary /path/to/lg-runtime --tag rust-v0.154.0 --build-profile lg-api-only --version 'lg-runtime YOUR_VERSION' --output /path/to/new-runtime.json
```

该命令只本地核验和创建新清单：要求源码仓库干净、pin 为 HEAD 的祖先、tag 指向 pin、版本探测匹配，并拒绝覆盖已有清单。它不构建、不下载、不复制二进制。
源码 commit/tree 与二进制的关联属于登记者的构建声明；当前没有签名、编译证明或可复现构建校验。build_profile 也是标识，不是“已经完成 API-only 裁剪”的证明。

## 后续选择性吸收上游

每批引入需记录上游 commit、依赖、用途、LG 修改、验证和回退点；必要补丁放在 `core/patches`，避免直接破坏上游基线。
构建入口与首个构建补丁已落地，精简源码生成和通用补丁导入管理仍未完成，不能宣称完成 Runtime 分叉构建系统。
许可证及必要的上游来源声明必须保留。下一批与验收门槛见 [项目路线](roadmap.md)。

## 隔离构建与当前发布元数据问题

`scripts/build_codex_runtime.py` 从产品仓库之外的干净 Codex checkout 构建，默认要求 HEAD 等于 pin。传入 `--source-commit` 时，要求完整 commit、HEAD 精确匹配且 pin 是其祖先。脚本不下载工具链；使用源码指定的 Rust 版本、`--locked` 和上游 `dev-small`／`release` profile，拒绝覆盖证据目录或将构建输出放进源码／产品目录。

构建记录包含源码 commit/tree、Cargo.lock 哈希、构建脚本哈希、工具链、命令、日志、产物版本及哈希。二进制复制到证据目录的 `bin/`，不引用可被下一次构建覆盖的 Cargo 缓存。结束前重新检查源码身份。成功状态为 `built_unvalidated`，不会自动登记或切换 Runtime；协议、工具和恢复检查是另外的验收步骤。该记录提供本地构建来源，不等于签名或可复现构建证明。

2026-10-15 实测原始 pin 的 `cargo metadata --locked` 失败：Cargo.toml 的工作区版本已为 `0.154.0`，Cargo.lock 中 150 个本地包仍为 `0.0.0`。诊断副本生成的差异只改变这 150 个本地版本；外部包版本、来源、checksum 及依赖关系逐项比较不变。

修正保存在 [构建补丁](../core/patches/0001-cargo-workspace-release-versions.patch) 和 [来源清单](../core/patches/0001-cargo-workspace-release-versions.json)。只在独立源码工作树应用，不能写入 `core/codex`。重放时核对上游 pin 与补丁哈希，先执行 `git apply --check`，再应用并形成独立 commit；重新生成的 commit 可以不同，但应核对预期 source tree 与锁文件哈希。不能将带此补丁的源码称为完全未经修改的原始基线。

```bash
# 从 LiteraryAgent 创建隔离源码；路径由操作者选择。
git worktree add --detach /path/to/source 6b9826e3aa83b1a5947db50f4332cb9c65f1b340
git -C /path/to/source apply --check /path/to/LiteraryAgent/core/patches/0001-cargo-workspace-release-versions.patch
git -C /path/to/source apply /path/to/LiteraryAgent/core/patches/0001-cargo-workspace-release-versions.patch
git -C /path/to/source add codex-rs/Cargo.lock
git -C /path/to/source commit -m 'build: align workspace lock versions with release'
git -C /path/to/source rev-parse HEAD

# 将上一步得到的完整 commit 传给 --source-commit。
python scripts/build_codex_runtime.py --source /path/to/source --source-commit DERIVED_COMMIT --output /path/to/new-evidence --cargo /path/to/cargo --jobs 4 --binary codex --binary codex-app-server
```

不指定 `--source-commit` 的原样检查仍会如实报告当前 pin 的锁文件问题。`--offline` 用于依赖缓存齐备后的离线构建；没有缓存时不能代替正常依赖获取。`--target-dir` 可指定外部复用缓存，证据目录每次必须新建。源码及依赖均未裁剪的此轮产物为 Linux GNU `dev-small` 开发构建，不能与已安装的 musl release 二进制直接比较性能或体积。

本机证据：`/opt/conda/envs/LitIsLand/audits/runtime-build-t2rtu008/`。仅版本元数据修正的派生 commit 为 `766b2a6e2fd6bddde905a080087c70c3d9d386e4`。CLI 首次编译 8 分 40 秒；脚本重放 CLI 并完成独立 App Server 构建。CLI 通过 32 项 Runtime 回归及 10 项 Extension 集成，独立 App Server 通过直接启动／配置校验、流式响应、进程重启恢复和通用补丁分发测试。模型响应使用本地合成服务，不代表真实文学质量验证。

`just bazel-lock-update` 已在另一隔离工作树执行；Java 下载器对 GitHub 和 BCR 注册表收到代理 407，补充 V8 官方归档到 distdir 后仍受注册表访问阻碍。该检查未通过，`MODULE.bazel.lock` 未变化，不能宣称 Bazel 基线完成。

## 最小入口的当前边界

Linux normal/build 依赖图中完整 CLI 为 1020 个包，独立 App Server 为 930 个包；云任务、TUI 等入口不在后者依赖图中。数量只是此平台、此依赖边界的证据，不代表文学质量或整套产品的裁剪收益。`login` 仍提供共享 AuthManager 与 API 凭据能力，不能整 crate 删除。

LG 当前仍按 `codex app-server`、`codex exec` 及 TUI 分发启动。独立 App Server 是直接启动的二进制，不支持这些 CLI 子命令；会话探针通过不代表全部 LG 入口兼容。因此本批没有更改 Runtime 发现规则、默认清单或删除 Rust 产品模块。后续先明确 app-server／exec／TUI 的能力与入口契约，再逐批裁剪已退出的产品依赖。
