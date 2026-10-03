# 保障货运司机劳动权益协同基础服务

本仓库包含两层服务：

1. **基础服务**（`src/transport_coordination/`）：运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。
2. **司机履约与权益保障项目**（`src/driver_rights/`）：面向监管与运输企业的司机履约、工时、结算、扣款、申诉与复算领域服务，构建在基础服务的身份、幂等、事务与审计边界之上。

## 司机履约与权益保障项目

针对货运司机权益热线的两类集中投诉（未满足休息要求仍被派单、月末用晚到异常记录追扣已结算运费），本项目提供：

- **可控时钟履约记录**：按可替换时钟记录驾驶、装卸、等待、休息区段，支持跨夜轮班；夜间窗口与跨夜次数按任务时区的当地日历计算。
- **任务级规则关联**：每趟任务在派单时快照司机合同版本（费率、最低结算、补贴、申诉时限）与工时监管规则版本；扣款必须引用同任务的证据。
- **派单前原子校验**：在同一事务内校验剩余驾驶/执勤工时与后续班次间最低休息窗口，不满足即拒绝且不留副作用。
- **只影响未冻结区段的变更**：双驾换班、任务取消、途中救援与补录事件均不能改写已冻结（已结算）的区段；已结束未结算的任务允许补录。
- **幂等与防重复计费**：所有写操作按 `request_id` 幂等；事件重放返回原回执；结算明细行按业务唯一键去重。
- **争议金额单独托管**：申诉期间争议金额进入托管台账，无争议收入照常支付；裁决后托管定向释放。
- **分角色视图**：司机看到可接任务与本人结算明细，承运人看到本企业结算与扣款证据，监管人员看到超时责任归因与申诉进度。
- **关账后复算**：规则调整后可对已关账账期复算，生成对比报告而不改动原结算单。

### 角色

在基础角色之上扩展：`driver`（司机）、`carrier`（承运人/调度）、`regulator`（监管人员）。基础角色 `admin`/`operator`/`reviewer`/`auditor` 保持不变。

### 主要接口（`/driver-rights` 前缀）

| 接口 | 说明 |
| --- | --- |
| `POST /drivers` | 登记司机档案（承运人） |
| `POST /regulations` | 发布工时监管规则版本（监管） |
| `POST /contracts` | 发布司机合同版本（承运人） |
| `POST /tasks` `/tasks/assign` `/tasks/accept` | 建单、原子校验派单、司机接单 |
| `POST /tasks/cancel` `/tasks/complete` | 取消/完成，仅影响未冻结区段 |
| `POST /events` | 区段开始/结束、双驾换班、途中救援、补录 |
| `POST /evidence` `/deductions` `/deductions/cancel` | 扣款证据与扣款 |
| `POST /settlements/run` | 任务结算（生成明细行并冻结区段） |
| `POST /periods/close` `/periods/recompute` | 账期关账 / 关账后复算 |
| `POST /appeals` `/appeals/resolve` | 司机申诉（托管）与监管裁决 |
| `POST /statements/pay` | 登记支付（不阻断无争议收入） |
| `GET /tasks/available` `/tasks/detail` | 司机可接任务 / 任务详情 |
| `GET /statements` `/statements/detail` | 结算明细（按角色过滤） |
| `GET /violations` `/appeals` `/recompute-reports` | 超时责任 / 申诉进度 / 复算报告 |

## 目录

- `src/transport_coordination/`：基础服务（领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收）；
- `src/driver_rights/`：司机履约项目（工时与结算纯函数、领域服务、HTTP 路由、离线验收）；
- `tests/`：基础规则、事务边界、接口路由、工时与结算纯函数、领域服务和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
PYTHONPATH=src python3 -m driver_rights.acceptance
```

两条验收命令都会在临时 SQLite 数据库中跑通各自业务链：基础服务核对登记、幂等回执与审计链；司机权益项目跑通跨夜双驾任务的派单、履约、结算、扣款、关账、申诉托管、支付与关账后复算，并验证两类投诉对应的保护（关账后不可追扣、冻结区段不可补录）。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m driver_rights.api --database driver_rights.sqlite3 --host 127.0.0.1 --port 8081
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。司机权益服务的 `/driver-rights` 前缀之外的路径会回退到基础服务路由，组织与操作者登记可直接复用。
