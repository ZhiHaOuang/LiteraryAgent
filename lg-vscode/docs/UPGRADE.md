# 版本与升级

当前配对：LG Python 产品包 0.3.0（须包含本次新增模块）、扩展 0.2.0、`codex-cli 0.154.0`。仅包版本相同不能证明安装含有新增接口，须先运行启动器 `--describe`。核心 commit：`6b9826e3aa83b1a5947db50f4332cb9c65f1b340`。

## app-server 升级

1. 在 LitIsLand 创建隔离副本，先运行产品仓库 `bash scripts/update_codex_core.sh --check`。不要直接修改 vendored 核心。
2. 为候选二进制生成新 manifest，记录绝对路径、版本、commit 和 SHA-256。不得把旧 manifest 的版本号改成新值来绕过校验。
3. 用候选二进制执行 `app-server generate-ts --out <独立目录>` 与 `app-server generate-json-schema --out <独立目录>`；比较请求、通知、审批 schema 和历史接口。检查 `src/backend/generated/PIN.json` 与使用的类型闭包。
4. 更新 Python 启动器版本校验、适配层描述校验和生成类型。Webview 自有契约独立演进，不直接导入新协议。
5. 运行传输、状态、Python 领域测试及真实内核集成测试，再完成拒绝、取消、恢复、文件保护与真实创作验收。未经验证不放宽版本检查。
6. 在测试作品上安装新 VSIX，检查已保存候选及审批状态。验证后才应用到正式产品。保留旧 VSIX、旧 runtime 和作品备份；回退不能撤销已经完成的作品写入。

## npm 与界面升级

使用提交的 `package-lock.json` 执行 `npm ci --ignore-scripts --no-audit --no-fund`。需要更新依赖时在沙箱显式更新清单与锁文件，重新进行构建、类型检查、回归和 Webview 检查。

`webview/ui` 是独立源码，不从外部 Qwen 目录动态导入。迁移来源文件映射保存在 `docs/import-files.json`，用于工程审计；不得重新引入原 CLI、ACP、daemon、账号或 prepackage 脚本。构建时从实际生产依赖生成 `THIRD_PARTY_NOTICES.md`；找不到声明的依赖会使构建失败。

## 发布边界

`npm run package` 只生成自用 VSIX，不上传 Marketplace。发布、自动更新服务和遥测均不属于本工程的交付动作。

## 0.1.0 升级到 0.2.0

先更新配套 Python 源码，再安装 0.2.0 VSIX 并 Reload Window。新增资料接口与 CLI 的 Studio 命令共用实现；核心二进制与 manifest 不变。核对 `studio library` 可读取已有作品，然后检查首页、人物来源、CLI 保存后的同步、候选预览和单次采用。保留 0.1.0 VSIX；回退扩展不会撤销已经在 ProjectStore 中采用的资料。模型账号、作品库、已有正文不需要迁移或删除。
