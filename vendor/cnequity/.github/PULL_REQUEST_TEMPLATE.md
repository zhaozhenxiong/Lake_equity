## 摘要

<!-- 改了什么、为什么（1–3 条）。 -->

-

## 检查清单

- [ ] 行为变更时补充/更新测试（`pytest tests/unit` 仍保持离线）
- [ ] 用户可见变更已更新文档 / 数据集目录 / CHANGELOG
- [ ] 未提交本地配置、湖数据或日志
- [ ] 新增网络 I/O 放在 adapters；schema/主键在 `domain/` 中声明
- [ ] 已说明与有效 ADR 的关系；改变保证时在同一 PR 标记 supersession、实现状态和迁移/回滚
- [ ] 数据契约变化已与上个稳定 release 比较；有意 breaking change 已升级 schema 并提供迁移说明
