# 普惠托位供给测算

面向区域服务规划的后端: 接收人口网格聚合需求、场地合规结论、机构备案、
班型容量、价格承诺与补助政策版本, 形成**按月可追溯**的供需与资金测算。
`domain.json` 是托位类型、机构状态、政策动作的唯一词汇准绳, 代码在写入时校验。

## 运行

```bash
python3 service.py --check          # 校验领域词汇并建库
python3 -m unittest -v              # 行为测试(23 例)
python3 service.py --port 8000      # 启动服务
```

环境变量: `CHILDCARE_DB`(SQLite 路径, 默认 `childcare.db`)、
`CHILDCARE_SECRET`(家庭标识散列密钥, 生产必须覆盖默认值)、
`CHILDCARE_TOKENS`(JSON, `{令牌: {"role": 角色[, "provider_id": ..]}}`,
缺省提供 `dev-planner/health/finance/auditor` 四个开发令牌)。
请求通过 `X-Token` 头鉴权。金额单位均为**分**, 月份格式 `YYYY-MM`。

## 角色与数据最小化

| 角色 | 职责范围 |
|---|---|
| planner(规划) | 需求/场地/阶段上报, 触发测算, 查看缺口与建设建议 |
| health(卫健) | 合规结论、机构备案与状态、服务范围、容量、价格、分配、候补登记 |
| finance(财政) | 政策版本发布, 补助确认与追回, 资金台账 |
| provider(机构) | 仅本机构的容量申报与价格承诺 |
| auditor(审计) | 只读缺口、资金、建议与审计日志 |

家庭数据只保留测算所需信息: 候补登记仅接受
`month/class_type/grid/street/family_ref` 五个字段(多传即 400),
`family_ref` 入库前即用 HMAC 散列为不可逆伪名, 原文与伪名都不出现在
任何查询接口与审计日志中, 对外只有聚合计数。

## 核心业务规则

- **分阶段启用**: 场地容量 = 当月已启用各阶段容量之和(`site_phases`);
  有效容量 = min(机构申报容量, 场地合规容量), 场地不合格则为 0。
- **跨街道服务**: 机构容量按 `service_areas` 权重分摊到各街道计入供给,
  缺省只服务场地所在街道。
- **候补匿名去重**: 同一家庭(伪名)同月同班型重复登记自动合并为一条。
- **临时停办**: 状态为"暂停"并附预计恢复月, 到期自动恢复;
  停办月不计供给、不发运营类补助、不能分配托位。退出为终态。
- **并发容量守卫**: 所有写操作在 `BEGIN IMMEDIATE` 事务内完成;
  分配、下调申报容量、下调场地阶段容量都会校验
  "已分配托位 ≤ 合规上限", 并发下不会超分。
- **政策追溯生效**: 政策版本含 `applies_from`(适用起始月)与
  `published_month`(发布月); 重算历史月份时自动选用当时应适用的最新版本,
  与已确认台账对冲后生成**补差**或**补助追回**行, 旧快照全部保留。
- **补助追回**: 财政可对已确认补助发起追回(重复申报/违约/停业等),
  追回总额不得超过该键已确认净额。
- **重复申报防护**: 同一快照内补助行唯一; 只有最新快照的测算行可确认,
  旧快照行自动废止, 应发额始终与已确认净额对冲, 不会重复发放。
- **价格承诺**: 运营补助要求当月有价格承诺且不超过政策 `price_cap`;
  中途调价按新生效月另起一行, 历史保留。

## 主要接口

```
POST /demand                        网格聚合需求上报 {records:[...]}
POST /sites                         登记场地
PUT  /sites/{id}/compliance         登记合规结论
PUT  /sites/{id}/phases             分阶段启用(phase/start_month/end_month/class_type/capacity)
POST /providers                     机构登记(初始状态: 规划)
POST /providers/{id}/status         状态变更(暂停可附 end_month)
PUT  /providers/{id}/service-areas  跨街道服务范围 {areas:[{street,weight}]}
PUT  /providers/{id}/capacity       申报班型容量(按月生效)
POST /providers/{id}/prices         价格承诺(分/月)
POST /providers/{id}/allocations    分配托位(并发守卫)
POST /waitlist                      匿名候补登记
GET  /waitlist/summary              候补聚合计数
POST /policies                      发布政策版本
POST /months/{m}/calculate          月度测算(生成新快照)
GET  /months/{m}/gaps               街道×班型供需缺口
GET  /months/{m}/funding            资金台账(测算行+已确认净额)
POST /subsidies/{id}/confirm        确认补助
POST /subsidies/{id}/clawback       补助追回
GET  /subsidies/{id}/explain        解释: 采用了哪版政策、计费输入、关联追回
GET  /recommendations?month=        建设建议列表
GET  /recommendations/{id}/explain  解释: 缓解了哪些网格的多少缺口
GET  /audit-log                     操作审计(仅审计角色)
GET  /health                        健康检查(公开)
```

## 代码结构

```
domain.json        领域词汇(托位类型/机构状态/政策动作)
service.py         入口: --check 与 HTTP 服务
childcare/domain.py    词汇校验与机构状态机
childcare/db.py        SQLite schema、WAL 与 IMMEDIATE 事务
childcare/store.py     数据访问与容量守卫
childcare/calc.py      月度测算引擎(缺口/建议/补助/追溯/追回)
childcare/security.py  家庭标识 HMAC 伪名化
childcare/api.py       路由、RBAC 与字段最小化校验
test_childcare.py      行为测试
```
