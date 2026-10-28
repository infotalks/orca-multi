---
id: T-YYYYMMDD-short-name
title: 一句话说明任务
# 只读任务写 allowed_files: []
allowed_files:
  - src/example/**
forbidden:
  - src/example/generated/**
verify:
  - npm run test
# 可选：
# workspace: auto        # auto | main | worktree
# reuse: T-...           # 复用前一个任务的工作区和终端
# base: <commit>         # 新 worktree 的基线，默认主工作区 HEAD
# agent: codex          # 或 zcode（zcode 用自身配置的模型，下面的 model/effort/tier 不生效）
# model: gpt-6.1-sol
# tier: FAST | MEDIUM | HEAVY   # 推理强度：FAST→low，默认 MEDIUM→medium，HEAVY→high
# effort: medium         # 直接指定时优先于 tier
---
## 目标
做什么、给谁用、做到什么算完成。

## 不做什么
明确排除项。

## 验收标准
- 可自动判断的条目（与 verify 对应）

## 参考
- 相关文件、文档、决策编号
