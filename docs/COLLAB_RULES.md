# Claude + Codex 协作规则 v1.0

> 适用于 AI Agent 桥接协同项目。基于 DEVICE LINK 项目复盘制定。

## 角色定义

| 角色 | 职责 | 不可做 |
|------|------|--------|
| **主开发（Claude）** | 架构设计、核心编码、代码审查、ADR 决策记录 | — |
| **测试/打包（Codex）** | 平台实测、打包 exe、GUI 验证、性能/长稳 | 架构级决策（需与主开发确认） |
| **测试修复** | Codex 可修 Bug 并推回 | 修完后**必须走 commit + push**，写明根因和修复；主开发可随时调整 |

## 版本控制（最高优先级）

1. **项目启动即 git init**，禁止文件覆盖式同步
2. 每次修改 **必须 commit**，commit message 格式：`fix(模块): 描述 [ADR-NNN]`
3. 双方通过 `git pull/push` 同步代码，**不走文件覆盖**
4. 仓库远端为 GitHub（或双方可达的 Git remote）
5. 运行数据（`data/` `logs/` `dist/` 生成的 `config.yaml`）**永不进仓库**

## 同步白名单

```
同步: src/ config/default_config.yaml assets/ tests/ docs/ build/
排除: data/ logs/ dist/ config/config.yaml __pycache__/ .pytest_cache/
```

同步前检查目标文件是否被进程占用（避免 btree 损坏）。

## 通信协议

1. **桥接文件统一 UTF-8 纯文本**，文件名英文 kebab-case
2. 消息头必须包含：`FROM:` `TO:` `主题:`
3. 紧急问题走 SSH 直接调用，非紧急走桥接文件
4. 每轮对话开始前检查对方来信
5. 写文件后不依赖对方立即回复（异步通信）

## Bug 处理流程

```
发现 Bug → 复现确认 → 定位根因 → 写 ADR → 修复 → commit → push → 对方 review
```

- 每个 Bug 必须在 `docs/adr/` 有对应记录（问题-根因-决策-理由）
- 修复后必须全量测试通过才能推回
- 对方 review 通过后关闭

## 代码质量标准

1. 全量测试通过（`pytest tests/`）才能 commit
2. 核心模块覆盖率 ≥ 80%
3. 每个公开方法有中文注释（参数/返回值/异常）
4. 修复 Bug 时必须加回归测试

## 安全与凭据

1. 密码/token/API key **不进仓库**，不落明文配置文件
2. 配置文件放占位符，真实凭据走环境变量或加密存储
3. 日志和桥接文件中的凭据必须脱敏
4. GitHub 认证用 SSH 公钥，不用一次性 token

## 复盘节奏

- 项目结束后双方各写复盘，合并后更新本规则
- 每条规则必须附带"为什么"（来自实际踩坑经验）
